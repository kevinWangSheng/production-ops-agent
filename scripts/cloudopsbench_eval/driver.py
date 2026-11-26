"""Cloud-OpsBench drivers: OpsPilot attempts, the bundled-agent control, scoring (#871).

Contract r1 (``docs/tasks/2026-11-26-cloudopsbench-contract.md``). One
OpsPilot attempt of one case:

1. a fresh snapshot view of the case (``views.build_view``, A2); the
   question from the case's ``query`` and ``namespace`` only (U2);
2. the benchmark's MCP server on the view (its own venv, ``backend``), whose
   ``tools/list`` must equal the pinned catalog; a fresh PostgreSQL; the
   product web and worker through ``product_entry`` with the
   ``cloudopsbench`` tool profile (U1); all on 127.0.0.1;
3. the question submitted as a manual question; the Run awaited (the
   product's default limits, U4), frozen with ``scripts.run_freeze`` into the
   attempt directory, its model inputs checked for answer markers (U7 4);
4. everything stopped in reverse order and the ports checked released; then
   the report converted (``answer``, U3) into the scorer's case file, with the
   server's JSONL trace as its tool steps (A4) and cache counts (A3).

The control (U6) runs the benchmark's bundled ReAct agent (``run.py``) under
its own venv, one case per process, ``deepseek-flash``, temperature 0, 20
iterations. ``score`` runs the benchmark's ``evaluation.py`` over a line and
freezes the round summary (A5, A6).

Usage (repository root, see the task record's runbook)::

    python -m scripts.cloudopsbench_eval.driver attempt --round <r> \\
        --checkout <Cloud-OpsBench> --cob-python <venv>/bin/python \\
        --env-file <private> --case boutique/codedefect/3 [--seq 1]
    python -m scripts.cloudopsbench_eval.driver round --round <r> ... \\
        --subset docs/evidence/cloudopsbench-871/subset.json [--shard 1/2]
    python -m scripts.cloudopsbench_eval.driver control --round <r> ... \\
        --subset <file>  [--case <id>]
    python -m scripts.cloudopsbench_eval.driver score --round <r> \\
        --line opspilot|control --checkout ... --cob-python ... --subset <file>

Paths: everything under ``tmp/cloudopsbench/<round>/`` (git-ignored); the
frozen summaries under ``docs/evidence/cloudopsbench-<round>/<line>/``.
Attempt directories are named by the case's position in the list
(``c001``), never by its id: the id names the fault category.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts import lab_evidence, run_freeze
from scripts.cloudopsbench_eval import answer, backend, catalog, question, scoring
from scripts.cloudopsbench_eval import profile as cob_profile
from scripts.cloudopsbench_eval import subset as subset_mod
from scripts.cloudopsbench_eval.views import (
    CaseRef,
    ViewError,
    build_view,
    read_question_fields,
)
from scripts.holmes_evals.driver import load_env_file, report_object
from scripts.holmes_evals.intake import new_account, submit_question
from scripts.itbench_lite import services
from scripts.itbench_lite.driver import (
    HEALTH_PROFILE_ID,
    Cleanup,
    _pg_start,
    _pg_stop,
    run_state,
)
from scripts.itbench_lite.services import ServiceError, Started

ROOT = Path(__file__).resolve().parents[2]
TMP_ROOT = ROOT / "tmp" / "cloudopsbench"
EVIDENCE_ROOT = ROOT / "docs" / "evidence"
LINE_OPSPILOT = "opspilot"
#: The control's scorer directory is its model name (``run.py`` layout).
CONTROL_MODEL = "deepseek-flash"
CONTROL_API_BASE = "https://api.deepseek.com/v1"
CONTROL_ITERATIONS = 20
CONTROL_TIMEOUT_S = 3600
CLUSTER_UID = "cloudopsbench"
SUMMARY_SCHEMA = "cloudopsbench-round-1"
RECORD = "attempt.json"
TERMINAL_WAIT_S = 7200 + 600  # the Run's wall time plus 10 minutes
POLL_S = 5.0
WORKER_SETTLE_S = 5.0
_ROUND = re.compile(r"^[a-z0-9][a-z0-9.-]{0,40}$")
MODEL_KEYS = ("DEEPSEEK_API_KEY",)
LANGSMITH_KEYS = ("LANGSMITH_API_KEY", "LANGSMITH_ENDPOINT", "LANGSMITH_WORKSPACE_ID")
#: Text that would mean an answer file or grading field reached the model.
LEAK_MARKERS = (
    "metadata.json",
    "milestone",
    "process-label",
    "golden-trajectory",
    "fault_taxonomy",
    "fault_object",
    "difficulty",
    "admissible_",
)


class DriverError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class Ports:
    pg: int = 55451
    web: int = 18081
    mcp: int = 18951

    def shifted(self, offset: int) -> Ports:
        return Ports(self.pg + offset, self.web + offset, self.mcp + offset)

    def all(self) -> dict[str, int]:
        return {"pg": self.pg, "web": self.web, "mcp": self.mcp}


@dataclass
class Config:
    round: str
    checkout: Path
    cob_python: Path
    ports: Ports = field(default_factory=Ports)
    trace: str = "off"
    secrets_env: dict[str, str] = field(default_factory=dict)
    wait_s: float = TERMINAL_WAIT_S
    git: dict[str, Any] = field(default_factory=dict)

    @property
    def round_dir(self) -> Path:
        return TMP_ROOT / self.round

    @property
    def answers_root(self) -> Path:
        return self.round_dir / "answers"

    @property
    def project_name(self) -> str | None:
        return (
            lab_evidence.lab_project(f"cob-{self.round}")
            if self.trace == "lab"
            else None
        )

    @property
    def experiment(self) -> str:
        return f"cloudopsbench-{self.round}"


@dataclass
class Backends:
    """Everything that touches the host; tests replace any of these."""

    start_pg: Callable[[Path, int], Any] = _pg_start
    stop_pg: Callable[[Any], None] = _pg_stop
    start_mcp: Callable[..., Started] = backend.start_mcp_server
    start_web: Callable[[Mapping[str, str], int, Path], Started] = (
        lambda env, port, log: _start_product("web", env, log, port=port)
    )
    start_worker: Callable[[Mapping[str, str], Path], Started] = lambda env, log: (
        _start_product("worker", env, log)
    )
    submit: Callable[..., Any] = submit_question
    run_state: Callable[[str, str], str | None] = run_state
    freeze: Callable[..., tuple[dict[str, Any], Path]] = run_freeze.freeze_run
    port_in_use: Callable[[int], bool] = services.port_in_use
    run: Callable[..., Any] = subprocess.run
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


def _start_product(
    role: str, env: Mapping[str, str], log_path: Path, *, port: int | None = None
) -> Started:
    proc = services.spawn(
        [sys.executable, "-m", "scripts.cloudopsbench_eval.product_entry", role],
        env,
        log_path,
    )
    started = Started(
        role, lambda: services.stop_process(proc), alive=lambda: proc.poll() is None
    )
    try:
        if port is not None:
            services.wait_tcp(
                port, timeout=services.READY_TIMEOUT_S, alive=started.alive
            )
        else:
            deadline = time.monotonic() + WORKER_SETTLE_S
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise ServiceError("WORKER_EXITED", f"rc={proc.returncode}")
                time.sleep(0.2)
    except BaseException:
        started.stop()
        raise
    started.info = {"pid": proc.pid}
    return started


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_json(path: Path, value: Any, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(value, indent=1, default=str) + "\n")
    if mode is not None:
        os.chmod(partial, mode)
    os.replace(partial, path)


# -- product environments -----------------------------------------------------------


def profile_env(ref: CaseRef) -> dict[str, str]:
    return {
        cob_profile.SYSTEM_ENV: ref.system,
        cob_profile.CATEGORY_ENV: ref.category,
        "OPSPILOT_HEALTH_BIND": "",
    }


def web_env(
    cfg: Config, ref: CaseRef, pg: Any, identities: Path, users: str
) -> dict[str, str]:
    origin = f"http://127.0.0.1:{cfg.ports.web}"
    return (
        services.base_env()
        | profile_env(ref)
        | {
            "OPSPILOT_WEB_DSN": pg.web_dsn,
            "OPSPILOT_UI_USERS": users,
            "OPSPILOT_ALLOWED_ORIGINS": origin,
            "OPSPILOT_AUTH_REVISION": "cloudopsbench",
            "OPSPILOT_BIND": f"127.0.0.1:{cfg.ports.web}",
            "OPSPILOT_SELF_HEAL_INTERVAL_SECONDS": "0",
            "OPSPILOT_TARGET_IDENTITIES": str(identities),
        }
    )


def worker_env(cfg: Config, ref: CaseRef, pg: Any) -> dict[str, str]:
    env = (
        services.base_env()
        | profile_env(ref)
        | {
            "OPSPILOT_WORKER_DSN": pg.runtime_dsn,
            cob_profile.MCP_URL_ENV: backend.mcp_url(cfg.ports.mcp),
            "OPSPILOT_TRACE": cfg.trace,
            "OPSPILOT_WORKER_CAPACITY": "1",
        }
    )
    env.update({k: cfg.secrets_env[k] for k in MODEL_KEYS if cfg.secrets_env.get(k)})
    if cfg.trace == "lab":
        env.update(
            {k: cfg.secrets_env[k] for k in LANGSMITH_KEYS if cfg.secrets_env.get(k)}
        )
        env["LANGSMITH_PROJECT"] = str(cfg.project_name)
    return env


def check_product_env(env: Mapping[str, str], forbidden: Sequence[str]) -> None:
    """No product process may hold a dataset path or the case id (A2)."""
    for key, value in env.items():
        for needle in forbidden:
            if needle and needle in value:
                raise DriverError("PRODUCT_ENV_LEAK", key)


def write_identities(path: Path, namespace: str) -> None:
    entry = {
        "integration_id": cob_profile.TARGET_ID,
        "cluster_uid": CLUSTER_UID,
        "namespace": namespace,
        "workload": namespace,
        "health_profile_id": HEALTH_PROFILE_ID,
        "match": {"version": 1, "labels": {"namespace": namespace}},
    }
    _write_json(path, {cob_profile.TARGET_ID: entry})


def leak_hits(text: str, extra: Sequence[str] = ()) -> list[str]:
    """Answer markers present in the model-input text (U7 4)."""
    return [m for m in (*LEAK_MARKERS, *extra) if m and m in text]


# -- one OpsPilot attempt -------------------------------------------------------------


class Attempt:
    def __init__(self, cfg: Config, b: Backends, case_id: str, seq: int) -> None:
        self.cfg, self.b = cfg, b
        self.ref = CaseRef.parse(case_id)
        self.seq = seq
        self.dir = cfg.round_dir / LINE_OPSPILOT / f"c{seq:03d}"
        self.cleanup = Cleanup()
        self.record: dict[str, Any] = {
            "round": cfg.round,
            "seq": seq,
            "case": self.ref.id,
            "ports": cfg.ports.all(),
            "git": cfg.git,
            "status": "started",
            "failure": None,
        }

    def _own(self, item: Started) -> Started:
        self.cleanup.push(item.name, item.stop)
        return item

    def run(self) -> dict[str, Any]:
        if self.dir.exists():
            shutil.rmtree(self.dir)
        self.dir.mkdir(parents=True)
        self.record["started_at"] = self.b.now().isoformat()
        report: Any = None
        try:
            report = self._steps()
        except (DriverError, ViewError, ServiceError) as error:
            self.record.update(status="failed", failure=error.code)
        except Exception as error:  # noqa: BLE001 - recorded, the round goes on
            self.record.update(
                status="failed", failure=f"CRASHED:{type(error).__name__}"
            )
        finally:
            errors = self.cleanup.close()
            if errors:
                self.record["cleanup_errors"] = errors
            self.record["ports_released"] = {
                name: not self.b.port_in_use(port)
                for name, port in self.cfg.ports.all().items()
            }
        self._answer(report)
        self.record["ended_at"] = self.b.now().isoformat()
        _write_json(self.dir / RECORD, self.record)
        return self.record

    def _steps(self) -> Any:
        cfg, b, ref = self.cfg, self.b, self.ref
        for name, port in cfg.ports.all().items():
            if b.port_in_use(port):
                raise DriverError("PORT_IN_USE", name)
        case_dir = ref.case_dir(cfg.checkout)
        view = self.dir / "view"
        self.record["view"] = build_view(case_dir, view)
        query, namespace = read_question_fields(case_dir)
        text = question.render_question(ref.system, query=query, namespace=namespace)
        (self.dir / "question.txt").write_text(text, encoding="utf-8")
        key = question.idempotency_key(cfg.round, self.seq)
        self.record.update(
            question_sha256=_sha256(text),
            question_chars=len(text),
            template_sha256=question.QUESTION_TEMPLATE_SHA256,
            idempotency_label=key,
        )
        mcp = self._own(
            b.start_mcp(
                cob_python=cfg.cob_python,
                checkout=cfg.checkout,
                env=backend.server_env(
                    view=view,
                    system=ref.system,
                    category=ref.category,
                    port=cfg.ports.mcp,
                    trace_path=self.dir / "mcp-trace.jsonl",
                    run_id=f"{cfg.round}-c{self.seq:03d}",
                ),
                port=cfg.ports.mcp,
                log_path=self.dir / "mcp.log",
            )
        )
        if not catalog.tools_match(
            mcp.info.get("tools") or [], ref.system, ref.category
        ):
            raise DriverError("TOOLS_MISMATCH")
        pg = b.start_pg(self.dir / "pg", cfg.ports.pg)
        self.cleanup.push("pg", lambda: b.stop_pg(pg))
        identities = self.dir / "target_identities.json"
        write_identities(identities, namespace)
        account = new_account()
        wenv = web_env(cfg, ref, pg, identities, account.users_env())
        kenv = worker_env(cfg, ref, pg)
        forbidden = [str(cfg.checkout), str(case_dir), ref.id]
        check_product_env(wenv, forbidden)
        check_product_env(kenv, forbidden)
        self._own(b.start_web(wenv, cfg.ports.web, self.dir / "web.log"))
        worker = self._own(b.start_worker(kenv, self.dir / "worker.log"))
        result = b.submit(
            f"http://127.0.0.1:{cfg.ports.web}",
            account,
            question=text,
            key=key,
            target_id=cob_profile.TARGET_ID,
        )
        self.record["intake"] = {"status": result.status, "code": result.code}
        if result.status != 201 or not result.run_id:
            raise DriverError("INTAKE_REFUSED", str(result.code))
        run_id = str(result.run_id)
        self.record["run_id"] = run_id
        began = b.monotonic()
        state: str | None = None
        while True:
            state = b.run_state(pg.owner_dsn, run_id)
            if state not in (None, "queued", "running"):
                break
            if not worker.alive() or b.monotonic() - began >= cfg.wait_s:
                break
            b.sleep(POLL_S)
        self.record["run_wait"] = {
            "terminal_state": state,
            "timed_out": state in (None, "queued", "running"),
            "seconds": round(b.monotonic() - began, 3),
            "worker_alive": worker.alive(),
        }
        worker.stop()
        frozen, _ = b.freeze(
            pg.owner_dsn,
            run_id,
            experiment=cfg.experiment,
            evidence_dir=self.dir / "freeze",
            project=cfg.project_name,
            client_factory=lambda: lab_evidence.langsmith_client(cfg.secrets_env),
        )
        block = frozen.get("run") or {}
        self.record["run"] = {
            "state": (block.get("run") or {}).get("state"),
            "model_requests_used": (block.get("run") or {}).get("model_requests_used"),
            "tool_requests": len(block.get("tool_requests") or []),
            "tokens": block.get("tokens"),
            "report_present": block.get("report_present"),
            "versions": block.get("versions"),
        }
        self.record["trace"] = frozen.get("trace")
        ledger_path = lab_evidence.ledger_dir(cfg.experiment, run_id) / "ledger.json"
        ledger_text = (
            ledger_path.read_text(encoding="utf-8") if ledger_path.is_file() else ""
        )
        self.record["leak_check"] = {
            "ledger_read": bool(ledger_text),
            "hits": leak_hits(ledger_text, [ref.id, str(cfg.checkout)]),
        }
        if self.record["run_wait"]["timed_out"]:
            raise DriverError("RUN_NOT_TERMINAL")
        report, reason = report_object(block)
        if report is None:
            raise DriverError(reason or "NO_REPORT")
        self.record["status"] = "ok"
        return report.model_dump(mode="json")

    def _answer(self, report: Any) -> None:
        """Always writes the case file: a failed attempt has no final answer
        and scores 0 (A7)."""
        trace_path = self.dir / "mcp-trace.jsonl"
        lines = (
            trace_path.read_text(encoding="utf-8").splitlines()
            if trace_path.is_file()
            else []
        )
        events = answer.read_trace(lines)
        self.record["cache"] = answer.cache_stats(events)
        converted = (
            answer.convert(report, self.ref.system) if report is not None else None
        )
        self.record["conversion"] = answer.conversion_record(converted)
        path = scoring.answer_path(self.cfg.answers_root, LINE_OPSPILOT, self.ref)
        _write_json(path, answer.case_file(converted, answer.trajectory_steps(events)))


# -- the control (bundled ReAct agent) ----------------------------------------------------


def control_config(cfg: Config, ref: CaseRef, api_key: str) -> str:
    rows = {
        "model": {
            "model": CONTROL_MODEL,
            "provider": "openai_compatible",
            "api_base": CONTROL_API_BASE,
            "api_key": api_key,
            "temperature": 0,
            "max_tokens": 4096,
            "timeout": 60,
        },
        "diagnosis": {
            "max_iterations": CONTROL_ITERATIONS,
            "system": ref.system_dir,
            "fault_category": ref.category,
            "case_name": ref.number,
            "dataset_root": str(cfg.checkout),
            "save_root": str(cfg.answers_root),
        },
    }
    lines = []
    for section, values in rows.items():
        lines.append(f"{section}:")
        lines += [f"  {k}: {json.dumps(v)}" for k, v in values.items()]
    return "\n".join(lines) + "\n"


def control_case(cfg: Config, b: Backends, case_id: str, seq: int) -> dict[str, Any]:
    """One case of the bundled agent, in a scratch copy of its code (the
    checkout is not written). The model key is in the copy's config file
    (mode 600) only while the process runs."""
    ref = CaseRef.parse(case_id)
    work = cfg.round_dir / "control" / f"c{seq:03d}"
    if work.exists():
        shutil.rmtree(work)
    agent = work / "agent"
    shutil.copytree(
        cfg.checkout / "agents" / "cloudops_agent",
        agent,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    config = agent / "configs" / "model_configs.yaml"
    trace = scoring.answer_path(cfg.answers_root, CONTROL_MODEL, ref)
    if trace.parent.exists():
        shutil.rmtree(trace.parent)
    record: dict[str, Any] = {"round": cfg.round, "seq": seq, "case": ref.id}
    key = cfg.secrets_env.get("DEEPSEEK_API_KEY", "")
    config.unlink(missing_ok=True)  # the copied file's mode would be kept
    fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(control_config(cfg, ref, key))
    began = b.monotonic()
    try:
        with (work / "control.log").open("wb") as log:
            done = b.run(
                [str(cfg.cob_python), "-I", str(agent / "run.py")],
                cwd=agent,
                env=services.base_env(),
                stdout=log,
                stderr=log,
                timeout=CONTROL_TIMEOUT_S,
                check=False,
            )
        record["exit_code"] = done.returncode
    except subprocess.TimeoutExpired:
        record["exit_code"] = None
        record["failure"] = "CONTROL_TIMEOUT"
    finally:
        config.write_text(control_config(cfg, ref, ""))
    record["seconds"] = round(b.monotonic() - began, 3)
    state: dict[str, Any] = {}
    if trace.is_file():
        state = json.loads(trace.read_text(encoding="utf-8"))
    steps = state.get("steps") or []
    record.update(
        stop_reason=state.get("stop_reason"),
        steps_used=len(steps),
        tool_calls=sum(1 for s in steps if s.get("action_type") == "tool"),
        tokens={
            "input_tokens": sum(int(s.get("input_tokens") or 0) for s in steps),
            "output_tokens": sum(int(s.get("output_tokens") or 0) for s in steps),
        },
    )
    if not trace.is_file():
        record.setdefault("failure", "CONTROL_NO_TRACE")
        _write_json(trace, answer.case_file(None, []))
    _write_json(work / RECORD, record)
    return record


# -- scoring and the frozen summary --------------------------------------------------------


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def cost_usd(
    tokens: Mapping[str, Any], prices: Mapping[str, float] | None
) -> float | None:
    """token x unit price (A8); ``prices`` per million tokens."""
    if not prices or not isinstance(tokens, Mapping):
        return None
    hit = int(tokens.get("prompt_cache_hit_tokens") or 0)
    miss = int(tokens.get("prompt_cache_miss_tokens") or 0)
    if not (hit or miss):
        miss = int(tokens.get("prompt_tokens") or tokens.get("input_tokens") or 0)
    out = int(tokens.get("completion_tokens") or tokens.get("output_tokens") or 0)
    return round(
        (
            hit * prices["cache_hit"]
            + miss * prices["cache_miss"]
            + out * prices["output"]
        )
        / 1_000_000,
        6,
    )


def round_summary(
    *,
    cfg: Config,
    line: str,
    cases: Sequence[str],
    scores: Mapping[str, Mapping[str, Any]],
    records: Mapping[str, Mapping[str, Any]],
    strata: Mapping[str, str] | None,
    prices: Mapping[str, float] | None,
    subset_sha256: str,
) -> dict[str, Any]:
    """The frozen round summary (A6): no predicted values, no answers."""
    rows = []
    for seq, case_id in enumerate(cases, 1):
        rec = records.get(case_id) or {}
        tokens = (
            (rec.get("run") or {}).get("tokens")
            if line == LINE_OPSPILOT
            else rec.get("tokens")
        )
        rows.append(
            {
                "seq": seq,
                "case": case_id,
                "status": rec.get("status")
                or ("ok" if not rec.get("failure") else "failed"),
                "failure": rec.get("failure"),
                "metrics": scores.get(case_id, {}).get("metrics"),
                "scorer_error": scores.get(case_id, {}).get("error"),
                "cost_usd": cost_usd(tokens or {}, prices),
                **(
                    {
                        "run": rec.get("run"),
                        "cache": rec.get("cache"),
                        "conversion": rec.get("conversion"),
                        "leak_check": rec.get("leak_check"),
                        "trace": rec.get("trace"),
                        "run_wait": rec.get("run_wait"),
                    }
                    if line == LINE_OPSPILOT
                    else {
                        "stop_reason": rec.get("stop_reason"),
                        "steps_used": rec.get("steps_used"),
                        "tool_calls": rec.get("tool_calls"),
                        "tokens": rec.get("tokens"),
                    }
                ),
            }
        )
    aggregate: dict[str, Any] = {}
    for metric in scoring.METRICS:
        values = [float(r["metrics"][metric]) for r in rows if r["metrics"]]
        entry: dict[str, Any] = {"mean": _mean(values)}
        if strata is not None and metric in ("CA", "FA", "JRA"):
            per = {r["case"]: float(r["metrics"][metric]) for r in rows if r["metrics"]}
            estimate = subset_mod.weighted_mean(per, strata)
            entry["weighted_full_set"] = (
                None if estimate is None else round(estimate, 4)
            )
        aggregate[metric] = entry
    by_system: dict[str, Any] = {}
    for system_dir in ("boutique", "trainticket"):
        picked = [
            r for r in rows if r["case"].startswith(system_dir + "/") and r["metrics"]
        ]
        by_system[system_dir] = {
            "n": len(picked),
            **{
                m: _mean([float(r["metrics"][m]) for r in picked])
                for m in ("CA", "FA", "JRA")
            },
        }
    costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
    return {
        "schema": SUMMARY_SCHEMA,
        "round": cfg.round,
        "line": line,
        "frozen_at": datetime.now(UTC).isoformat(),
        "git": cfg.git,
        "subset_sha256": subset_sha256,
        "question_template_sha256": question.QUESTION_TEMPLATE_SHA256,
        "tool_catalog_sha256": hashlib.sha256(
            catalog.CATALOG_FILE.read_bytes()
        ).hexdigest(),
        "conversion": "scripts.cloudopsbench_eval.answer (U3 = a, deterministic)"
        if line == LINE_OPSPILOT
        else "bundled agent's own Submit JSON",
        "limits": {
            "opspilot": "product default: 100 model requests per Run (U4)",
            "control": f"{CONTROL_ITERATIONS} iterations, temperature 0 (U6)",
        },
        "process_metrics_note": "MC/EOC/ECR/EE of the OpsPilot line come from the MCP trace and are approximate (A4)",
        "prices_per_million_usd": dict(prices) if prices else None,
        "cost_usd_total": round(sum(costs), 6) if costs else None,
        "count": len(rows),
        "aggregate": aggregate,
        "by_system": by_system,
        "cases": rows,
    }


# -- CLI ---------------------------------------------------------------------------------------


def git_state(allow_dirty: bool) -> dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True
        ).stdout

    changed = [
        line[3:]
        for line in git("status", "--porcelain", "--untracked-files=no").splitlines()
        if not line[3:].startswith("docs/evidence/cloudopsbench-")
    ]
    if changed and not allow_dirty:
        raise DriverError("WORKTREE_DIRTY", ",".join(changed[:5]))
    return {"commit": git("rev-parse", "HEAD").strip(), "dirty": bool(changed)}


def load_subset(path: Path) -> tuple[list[str], str]:
    raw = path.read_bytes()
    data = json.loads(raw)
    cases = [CaseRef.parse(c).id for c in data["cases"]]
    return cases, hashlib.sha256(raw).hexdigest()


def _config(args: argparse.Namespace) -> Config:
    if not _ROUND.match(args.round):
        raise DriverError("ROUND_INVALID")
    cfg = Config(
        round=args.round,
        checkout=args.checkout.resolve(),
        cob_python=args.cob_python,
        ports=Ports().shifted(args.port_offset),
        trace=args.trace,
        wait_s=args.wait_seconds,
    )
    if args.command != "score":
        cfg.secrets_env = {
            k: v
            for k, v in load_env_file(args.env_file).items()
            if k in (*MODEL_KEYS, *LANGSMITH_KEYS)
        }
        if "DEEPSEEK_API_KEY" not in cfg.secrets_env:
            raise DriverError("DEEPSEEK_API_KEY_MISSING")
    cfg.git = git_state(args.allow_dirty)
    if cfg.trace == "lab" and args.command in ("attempt", "round"):
        if "LANGSMITH_API_KEY" not in cfg.secrets_env:
            raise DriverError("LANGSMITH_API_KEY_MISSING")
        lab_evidence.ensure_longlived_project(
            lab_evidence.langsmith_client(cfg.secrets_env), str(cfg.project_name)
        )
    return cfg


def _selected(args: argparse.Namespace) -> list[tuple[int, str]]:
    if args.case:
        return [(args.seq, CaseRef.parse(args.case).id)]
    cases, _ = load_subset(args.subset)
    pairs = list(enumerate(cases, 1))
    if args.shard:
        index, count = (int(x) for x in args.shard.split("/"))
        pairs = [p for p in pairs if (p[0] - 1) % count == index - 1]
    return pairs


def _records(cfg: Config, line: str, cases: Sequence[str]) -> dict[str, dict[str, Any]]:
    folder = "opspilot" if line == LINE_OPSPILOT else "control"
    records = {}
    for seq, case_id in enumerate(cases, 1):
        path = cfg.round_dir / folder / f"c{seq:03d}" / RECORD
        if path.is_file():
            records[case_id] = json.loads(path.read_text())
    return records


def main(argv: Sequence[str] | None = None, backends: Backends | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.cloudopsbench_eval.driver")
    parser.add_argument("command", choices=("attempt", "round", "control", "score"))
    parser.add_argument("--round", required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--cob-python", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--subset", type=Path)
    parser.add_argument("--case")
    parser.add_argument("--seq", type=int, default=1)
    parser.add_argument("--shard", help="i/n: every n-th case of the list from i")
    parser.add_argument("--port-offset", type=int, default=0)
    parser.add_argument("--trace", choices=("lab", "off"), default="lab")
    parser.add_argument("--wait-seconds", type=float, default=TERMINAL_WAIT_S)
    parser.add_argument("--line", choices=(LINE_OPSPILOT, "control"))
    parser.add_argument(
        "--prices", help="cache_hit,cache_miss,output USD per 1M tokens"
    )
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument(
        "--resume", action="store_true", help="skip cases with a record"
    )
    args = parser.parse_args(argv)
    b = backends or Backends()
    try:
        cfg = _config(args)
        if args.command in ("attempt", "round"):
            for seq, case_id in _selected(args):
                done = cfg.round_dir / LINE_OPSPILOT / f"c{seq:03d}" / RECORD
                if args.resume and done.is_file():
                    continue
                rec = Attempt(cfg, b, case_id, seq).run()
                print(
                    f"c{seq:03d} status={rec['status']} failure={rec['failure']}",
                    flush=True,
                )
            return 0
        if args.command == "control":
            for seq, case_id in _selected(args):
                done = cfg.round_dir / "control" / f"c{seq:03d}" / RECORD
                if args.resume and done.is_file():
                    continue
                rec = control_case(cfg, b, case_id, seq)
                print(
                    f"c{seq:03d} stop={rec.get('stop_reason')} failure={rec.get('failure')}",
                    flush=True,
                )
            return 0
        if args.subset is None or args.line is None:
            raise DriverError("SCORE_NEEDS_SUBSET_AND_LINE")
        cases, digest = load_subset(args.subset)
        line = LINE_OPSPILOT if args.line == LINE_OPSPILOT else CONTROL_MODEL
        scores = scoring.score(
            line=line,
            case_ids=cases,
            checkout=cfg.checkout,
            cob_python=cfg.cob_python,
            answers_root=cfg.answers_root,
            work_dir=cfg.round_dir / f"score-{args.line}",
            run=b.run,
        )
        prices = None
        if args.prices:
            hit, miss, out = (float(x) for x in args.prices.split(","))
            prices = {"cache_hit": hit, "cache_miss": miss, "output": out}
        summary = round_summary(
            cfg=cfg,
            line=LINE_OPSPILOT if args.line == LINE_OPSPILOT else "control",
            cases=cases,
            scores=scores,
            records=_records(cfg, args.line, cases),
            strata=subset_mod.read_strata(cfg.checkout),
            prices=prices,
            subset_sha256=digest,
        )
        out_path = (
            EVIDENCE_ROOT / f"cloudopsbench-{cfg.round}" / args.line / "summary.json"
        )
        _write_json(out_path, summary)
        print(
            f"summary -> {out_path.relative_to(ROOT)}; run scripts/check_secrets.py before committing (A9)"
        )
        return 0
    except DriverError as error:
        print(f"driver: {error.code}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
