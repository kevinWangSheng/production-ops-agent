"""Holmes side of the same-question, same-model comparison (#668).

Runs the *upstream* HolmesGPT pytest harness (``tests/llm/test_ask_holmes.py``
at ``a0009195``, official configuration: the question's own ``toolsets.yaml``
plus upstream's ``default_toolsets.yaml``, R8-15) once per question and
trial, with OpsPilot's model (``scripts.holmes_baseline.config.MODEL`` and
``MODEL_ENV``, as F14), OpsPilot's question list, R8-13 and R10-1 applied by
the plugin in ``pytest_plugin/``. Upstream code is not modified.

    python -m scripts.holmes_compare.runner round --round R --upstream DIR \\
        --holmes-python DIR/.venv/bin/python --env-file F \\
        [--judge-path opspilot|upstream] [--questions 254,275] [--trials 1,2,3] \\
        [--kubeconfig F]
    python -m scripts.holmes_compare.runner scores --round R

``--judge-path`` defaults to ``opspilot`` (lead ruling): upstream's judge
text judged by OpsPilot's #666 judge (``scripts.holmes_evals.judge_run``),
the same judge for both agents. ``upstream`` (optional comparison) uses
upstream's autoevals classifier pointed at the same receiver and model.

C5 questions (#666 PR-C): Loki questions reach their own Loki through
upstream's own port-forwards (``toolsets.yaml`` addresses); each attempt of a
VictoriaLogs question gets a fresh VictoriaLogs container, given to the
harness as ``VICTORIALOGS_URL=http://localhost:<port>``; 100a/101/259_loki
wait for a whole-day offset before pytest starts and are shifted by the same
rule as OpsPilot's driver; after each attempt the question's namespace must
be gone and its ports free (the ES questions keep the index purge).

Raw files: ``tmp/holmes-compare/<round>/<qid>/trial-<n>/chance-<k>/`` (judge
input, the plugin's ``harness.json``, the shift record; the pytest log and
the judge rationale under ``answer-derived/``). Evidence:
``docs/evidence/holmes-compare-<round>/<qid>/trial-<n>/summary.json`` with
fields only (no answer, rationale, judge input or credential). Secrets come
only from ``--env-file`` and are never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.holmes_baseline.config import MODEL, MODEL_ENV
from scripts.holmes_compare.records import (
    COMPARE_SCHEMA,
    HOLMES_BLOCK,
    classify_holmes,
    load_rows,
)
from scripts.holmes_evals.judge import JUDGE_MODEL, ZAI_BASE, ZAI_KEY_NAMES
from scripts.holmes_evals.log_backends import (
    KNOWLEDGE_QUESTIONS,
    LOG_QUESTIONS,
    RELEASE_WAIT_S,
    VICTORIALOGS_IMAGE,
    backend_of,
    namespace_gone,
    namespaces_with_prefix,
    start_victorialogs,
)
from scripts.holmes_evals.questions import (
    ALL_QUESTIONS,
    B1_QUESTIONS,
    R8_13_SKILL_ASSERTION_DROPPED,
    UPSTREAM_COMMIT,
    UpstreamMismatch,
    question_dir,
    run_mode,
    verify_upstream,
)
from scripts.holmes_evals.scoring import score
from scripts.itbench_lite.services import ServiceError, base_env, port_in_use

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = Path(__file__).resolve().parent / "pytest_plugin"
PLUGIN = "opspilot_holmes_compare"
TRACK = "Holmes 对照（HolmesGPT 公开评测集，同题同模型）"
TOOL_FACE_NOTE = "Holmes 按官方默认配置运行，含 OpsPilot 按只读约束不提供的工具"
CHANCES = 3
#: One pytest session: setup (``setup_timeout`` up to 300 s in B1), Holmes'
#: run (OpsPilot's 7200 s Run wall clock, ``holmes_baseline.config``), judge.
PYTEST_TIMEOUT_S = 9000
DEEPSEEK_BASE = "https://api.deepseek.com/v1"
#: Upstream's root ``conftest.py`` ``responses`` fixture mocks every
#: ``requests`` call except a passthru list; Holmes' ES toolset uses
#: ``requests``, so the ES URL must be one of these (a0009195 conftest.py:269,
#: 289-293). ``http://127.0.0.1`` is *not* passed through.
ES_PASSTHRU = (
    re.compile(r"^http://localhost(?::\d+)?(?:/|$)"),
    re.compile(r"^https://[^/]*\.cloud\.es\.io(?:[:/]|$)"),
    re.compile(r"^https://[^/]*\.elastic-cloud\.com(?:[:/]|$)"),
    re.compile(r"^https://[^/]*\.es\.amazonaws\.com(?:[:/]|$)"),
)
JUDGE_PATHS = ("opspilot", "upstream")
#: Lead ruling 2026-11-19: upstream's judge text, #666 ``judge_trial`` (R8-3);
#: ``upstream`` (autoevals as shipped, endpoint and model swapped) is optional.
DEFAULT_JUDGE_PATH = "opspilot"
DISCLOSURES = (
    "TOOL_FACE_UPSTREAM_DEFAULT",
    "UPSTREAM_HARNESS_JUDGE_TEXT",  # intermediate outputs + tool-call block, upstream's own
    "QUESTION_TIME_REWRITTEN",
    "DESHIFT_TIME_ONLY_UNCHANGED",
    "DESHIFT_DATE_MIDPOINT",
)
#: PR-C: C5 questions replace the minute-shift deshift notes.
C5_DISCLOSURES = ("TOOL_FACE_UPSTREAM_DEFAULT", "UPSTREAM_HARNESS_JUDGE_TEXT")
LOKI_SHIFT_DISCLOSURES = (
    "QUESTION_TIME_REWRITTEN",
    "SHIFT_WHOLE_DAYS",
    "LOKI_ORIGINALS_KEPT",
    "LOKI_FUTURE_DROPPED",
    "LOG_LINE_TIME_REWRITTEN",
)
#: PR-D: the C2 questions; Holmes reads the knowledge fixtures straight from
#: the question directory as upstream does (CLI: ``*/SKILL.md``; server: the
#: mock DAL's ``runbook_*.json``), no review.
C2_DISCLOSURES = (
    "TOOL_FACE_UPSTREAM_DEFAULT",
    "UPSTREAM_HARNESS_JUDGE_TEXT",
    "KNOWLEDGE_UPSTREAM_DIRECT",
)
#: PR-F: the C6 database questions; Holmes uses each question's own admin
#: account from upstream's ``toolsets.yaml`` (OpsPilot a read-only one).
C6_DISCLOSURES = (
    "TOOL_FACE_UPSTREAM_DEFAULT",
    "UPSTREAM_HARNESS_JUDGE_TEXT",
    "DB_ADMIN_ACCOUNT_UPSTREAM",
)
DEFAULT_VICTORIALOGS_PORT = 29428


class RoundAbort(RuntimeError):
    def __init__(self, code: str, hint: str | None = None) -> None:
        self.code = code
        self.hint = hint
        super().__init__(code)


@dataclass
class Config:
    round: str
    upstream: Path
    holmes_python: Path
    judge_path: str
    questions: tuple[str, ...] = B1_QUESTIONS
    trials: tuple[int, ...] = (1, 2, 3)
    secrets: dict[str, str] = field(default_factory=dict, repr=False)
    kubeconfig: Path | None = None
    victorialogs_port: int = DEFAULT_VICTORIALOGS_PORT
    tmp_root: Path = ROOT / "tmp" / "holmes-compare"
    evidence_root: Path = ROOT / "docs" / "evidence"

    @property
    def evidence_dir(self) -> Path:
        return self.evidence_root / f"holmes-compare-{self.round}"

    def summary_path(self, qid: str, trial: int) -> Path:
        return self.evidence_dir / qid / f"trial-{trial}" / "summary.json"

    def trial_dir(self, qid: str, trial: int) -> Path:
        return self.tmp_root / self.round / qid / f"trial-{trial}"


def load_env_file(path: Path) -> dict[str, str]:
    from scripts.holmes_evals.driver import load_env_file as load

    return load(path)


def check_es_url(url: str) -> None:
    if any(p.match(url) for p in ES_PASSTHRU):
        return
    if re.match(r"^https?://(127\.0\.0\.1|\[::1\])(?::\d+)?(?:/|$)", url):
        raise RoundAbort(
            "ES_URL_USE_LOCALHOST",
            "upstream's responses fixture only passes http://localhost through; "
            "use http://localhost:<port> for the same ES",
        )
    raise RoundAbort("ES_URL_NOT_PASSTHRU")


def _zai_key(secrets: Mapping[str, str]) -> str:
    return next((secrets[n] for n in ZAI_KEY_NAMES if secrets.get(n)), "")


def preflight(cfg: Config) -> None:
    """Everything that would make the whole round meaningless."""
    if cfg.judge_path not in JUDGE_PATHS:
        raise RoundAbort("JUDGE_PATH_INVALID")
    if not cfg.round or not re.fullmatch(r"[A-Za-z0-9._-]+", cfg.round):
        raise RoundAbort("BAD_ROUND")
    if any(t not in range(1, 7) for t in cfg.trials):
        raise RoundAbort("BAD_TRIALS")
    unknown = [q for q in cfg.questions if q not in ALL_QUESTIONS]
    if unknown:
        raise RoundAbort("QUESTION_UNKNOWN")
    try:
        verify_upstream(cfg.upstream)
    except UpstreamMismatch as exc:
        raise RoundAbort(exc.code) from None
    if (cfg.upstream / ".env").exists():
        # pytest-dotenv / python-dotenv would load it into the harness.
        raise RoundAbort("UPSTREAM_DOTENV_PRESENT")
    if not cfg.holmes_python.is_file():
        raise RoundAbort("HOLMES_PYTHON_MISSING")
    needs_es = any(backend_of(q) == "elasticsearch" for q in cfg.questions)
    names = ["DEEPSEEK_API_KEY"]
    if needs_es:
        names += ["ELASTICSEARCH_URL", "ELASTICSEARCH_API_KEY"]
    for name in names:
        if not cfg.secrets.get(name):
            raise RoundAbort(f"{name}_MISSING")
    if not _zai_key(cfg.secrets):
        raise RoundAbort("JUDGE_KEY_MISSING")
    if needs_es:
        check_es_url(cfg.secrets["ELASTICSEARCH_URL"])
    if cfg.kubeconfig is None and any(
        backend_of(q) in ("kubernetes", "sql", "mongodb") and LOG_QUESTIONS[q].needs_k8s
        for q in cfg.questions
    ):
        # PR-D: 94/95/96/162 set up on a cluster; an unreachable one is the
        # harness's env_broken, not checked here.
        raise RoundAbort("KUBECONFIG_MISSING")


def pytest_env(cfg: Config, qid: str, chance_dir: Path) -> dict[str, str]:
    """The harness process environment, built from blank.

    Holmes gets the model key, the ES URL and engineer key (upstream's
    ``before_test`` and toolsets read them) and, in ``upstream`` judge mode
    only, the judge key as upstream's classifier expects it. In ``opspilot``
    mode upstream's classifier preflight is pointed at the model's own
    OpenAI-compatible endpoint, so the judge key never enters this process.
    """
    s = cfg.secrets
    backend = backend_of(qid)
    env = base_env(os.environ)
    env.update(MODEL_ENV)
    if backend == "elasticsearch":
        env.update(
            {
                "ELASTICSEARCH_URL": s["ELASTICSEARCH_URL"],
                "ELASTICSEARCH_API_KEY": s["ELASTICSEARCH_API_KEY"],
            }
        )
    elif backend in ("kubernetes", "sql", "mongodb"):
        # PR-D: no log backend, nothing to shift. PR-F: the database's
        # port-forward is upstream's harness's own (``port_forwards``).
        env["OHC_SHIFT_BACKEND"] = "none"
    else:
        spec = LOG_QUESTIONS[qid]
        # 259's generator stamps naive datetimes with the host zone.
        env["TZ"] = "UTC"
        if backend == "loki":
            env["OHC_SHIFT_BACKEND"] = "loki"
            assert spec.address is not None
            env["OHC_LOKI_URL"] = f"http://{spec.address}"
        else:
            env["OHC_SHIFT_BACKEND"] = "none"
            env["VICTORIALOGS_URL"] = f"http://localhost:{cfg.victorialogs_port}"
    env.update(
        {
            "MODEL": MODEL,
            "DEEPSEEK_API_KEY": s["DEEPSEEK_API_KEY"],
            "RUN_LIVE": "true",
            "ITERATIONS": "1",
            "ASK_HOLMES_TEST_TYPE": "cli",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(PLUGIN_DIR),
            "OHC_QID": qid,
            "OHC_TRIAL_DIR": str(chance_dir),
            "OHC_JUDGE": cfg.judge_path,
            "OHC_STEP_PYTHON": sys.executable,
            "OHC_REPO": str(ROOT),
            "OHC_DROP_SKILL": "1" if qid in R8_13_SKILL_ASSERTION_DROPPED else "0",
        }
    )
    if cfg.judge_path == "upstream":
        classifier = {
            "CLASSIFIER_MODEL": JUDGE_MODEL,
            "OPENAI_API_KEY": _zai_key(s),
            "OPENAI_API_BASE": ZAI_BASE,
            "OPENAI_BASE_URL": ZAI_BASE,
        }
    else:
        classifier = {
            "CLASSIFIER_MODEL": MODEL.split("/", 1)[1],
            "OPENAI_API_KEY": s["DEEPSEEK_API_KEY"],
            "OPENAI_API_BASE": DEEPSEEK_BASE,
            "OPENAI_BASE_URL": DEEPSEEK_BASE,
        }
    env.update(classifier)
    if cfg.kubeconfig is not None:
        env["KUBECONFIG"] = str(cfg.kubeconfig)
    return env


def pytest_argv(cfg: Config) -> list[str]:
    return [
        str(cfg.holmes_python),
        "-m",
        "pytest",
        "tests/llm/test_ask_holmes.py",
        "-p",
        PLUGIN,
        "-p",
        "no:cacheprovider",
        "-p",
        "no:sugar",
        "-n",
        "0",
        "--no-cov",
        "--tb=no",
        "-q",
        "-o",
        "log_cli=false",
    ]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _enable_todo(cfg: Config, qid: str) -> bool | None:
    """The question's ``enable_todo`` (a whitelisted loader field); None when
    the question cannot be loaded (the merge then has nothing to compare)."""
    from scripts.holmes_evals.questions import load_question

    try:
        return bool(load_question(cfg.upstream, qid).enable_todo)
    except Exception:  # noqa: BLE001 - a broken question dir is recorded elsewhere
        return None


def release_state(cfg: Config, qid: str, *, sleep: Any = time.sleep) -> dict[str, Any]:
    """C5: the question's namespace gone (up to ``RELEASE_WAIT_S``) and its
    host ports free; ``ok`` false is a failed purge."""
    spec = LOG_QUESTIONS[qid]
    env = base_env(os.environ)
    if cfg.kubeconfig is not None:
        env["KUBECONFIG"] = str(cfg.kubeconfig)
    gone = True
    if spec.namespace is not None:
        deadline = time.monotonic() + RELEASE_WAIT_S
        while not (gone := namespace_gone(spec.namespace, env)):
            if time.monotonic() >= deadline:
                break
            sleep(2)
    left: list[str] | None = None
    if gone and spec.namespace_prefix is not None:
        # PR-E (283): every ``app-283-*`` namespace gone; no answer from
        # kubectl is not released.
        deadline = time.monotonic() + RELEASE_WAIT_S
        while True:
            left = namespaces_with_prefix(spec.namespace_prefix, env)
            gone = left == []
            if gone or time.monotonic() >= deadline:
                break
            sleep(2)
    busy = [p for p in spec.host_ports if port_in_use(p)]
    state: dict[str, Any] = {
        "namespace": spec.namespace,
        "namespace_gone": gone,
        "busy_ports": busy,
        "ok": gone and not busy,
    }
    if spec.namespace_prefix is not None:
        # Public evidence: the count only (None: kubectl did not answer).
        state["namespaces_left"] = None if left is None else len(left)
    return state


def _purge(cfg: Config, qid: str) -> list[str]:
    if backend_of(qid) != "elasticsearch":
        state = release_state(cfg, qid)
        if not state["ok"]:
            raise RuntimeError("LOG_BACKEND_NOT_RELEASED")
        return []
    from scripts.holmes_evals.setup import purge_question_indices
    from scripts.holmes_evals.shift import EsAdmin

    admin = EsAdmin(
        cfg.secrets["ELASTICSEARCH_URL"], cfg.secrets["ELASTICSEARCH_API_KEY"]
    )
    return purge_question_indices(admin, qid)


def judge_opspilot(cfg: Config, qid: str, chance_dir: Path) -> dict[str, Any] | None:
    """OpsPilot's #666 judge on upstream's judge text, in its own process."""
    from scripts.holmes_evals.judge_run import judge_env

    out = chance_dir / "judge-result.json"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.holmes_evals.judge_run",
            "--upstream",
            str(cfg.upstream),
            "--qid",
            qid,
            "--text-file",
            str(chance_dir / "judge-input.txt"),
            "--rationale-dir",
            str(chance_dir / "answer-derived"),
            "--out",
            str(out),
        ],
        cwd=ROOT,
        env=judge_env({**base_env(os.environ), **cfg.secrets}),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    result = _read_json(out) if proc.returncode == 0 else None
    if not isinstance(result, Mapping):
        return {"choice": None, "passed": False, "error_code": "JUDGE_PROCESS_FAILED"}
    return dict(result)


def judge_upstream(harness: Mapping[str, Any]) -> dict[str, Any]:
    value = harness.get("upstream_score")
    choice = {1: "A", 0: "B"}.get(value) if isinstance(value, int) else None
    return {
        "choice": choice,
        "passed": choice == "A",
        "error_code": None if choice else "UPSTREAM_JUDGE_NO_SCORE",
    }


def shift_day_wait(cfg: Config, qid: str, *, sleep: Any = time.sleep) -> float:
    """100a/101/259_loki: wait for a whole-day offset (same rule as OpsPilot)."""
    from scripts.holmes_evals.questions import load_question
    from scripts.holmes_evals.setup import DEFAULT_SETUP_TIMEOUT
    from scripts.holmes_evals.shift import seconds_to_wait_for_shift_day

    timeout = load_question(cfg.upstream, qid).setup_timeout or DEFAULT_SETUP_TIMEOUT
    # pytest collection and harness start come before before_test.
    wait = seconds_to_wait_for_shift_day(qid, datetime.now(UTC), timeout + 120)
    if wait:
        sleep(wait)
    return wait


def run_attempt(cfg: Config, qid: str, chance_dir: Path) -> dict[str, Any]:
    (chance_dir / "answer-derived").mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        purged_before = _purge(cfg, qid)
    except Exception:  # noqa: BLE001 - classified, never printed
        return {"category": "env_broken", "code": "PURGE_FAILED"}
    backend = backend_of(qid)
    day_wait = 0.0
    if backend == "loki":
        try:
            day_wait = shift_day_wait(cfg, qid)
        except Exception as exc:  # noqa: BLE001 - ShiftError("SHIFT_WAIT_FOR_DAY")
            return {
                "category": "env_broken",
                "code": getattr(exc, "code", "SHIFT_WAIT"),
            }
    vlogs = None
    if backend == "victorialogs":
        try:
            vlogs = start_victorialogs(cfg.victorialogs_port, chance_dir)
        except ServiceError:
            return {"category": "env_broken", "code": "VICTORIALOGS_START_FAILED"}
    try:
        return _run_pytest(cfg, qid, chance_dir, started, purged_before, day_wait)
    finally:
        if vlogs is not None:
            vlogs.stop()


def _run_pytest(
    cfg: Config,
    qid: str,
    chance_dir: Path,
    started: float,
    purged_before: list[str],
    day_wait: float,
) -> dict[str, Any]:
    timed_out = False
    with (chance_dir / "answer-derived" / "pytest.log").open("wb") as log:
        try:
            proc = subprocess.run(
                pytest_argv(cfg),
                cwd=cfg.upstream,
                env=pytest_env(cfg, qid, chance_dir),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=PYTEST_TIMEOUT_S,
                check=False,
            )
            exit_code: int | None = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out, exit_code = True, None
    try:
        purged_after = _purge(cfg, qid)
    except Exception:  # noqa: BLE001
        raise RoundAbort("CLEANUP_FAILED") from None
    raw = _read_json(chance_dir / "harness.json")
    harness: Mapping[str, Any] | None = raw if isinstance(raw, Mapping) else None
    judge: dict[str, Any] | None = None
    if harness is not None and harness.get("captured"):
        judge = (
            judge_opspilot(cfg, qid, chance_dir)
            if cfg.judge_path == "opspilot"
            else judge_upstream(harness)
        )
    category, code = classify_holmes(harness, judge, timed_out=timed_out)
    props = (harness or {}).get("properties") or {}
    return {
        "category": category,
        "code": code,
        "pytest_exit": exit_code,
        "timed_out": timed_out,
        "wall_seconds": round(time.monotonic() - started, 3),
        "harness": harness,
        "judge": judge,
        "seconds": props.get("holmes_duration"),
        "tool_calls": props.get("tool_call_count"),
        "llm_calls": props.get("num_llm_calls"),
        "purged": {"before": purged_before, "after": purged_after},
        "shift_day_wait_seconds": round(day_wait, 1),
    }


def _judge_block(cfg: Config, judge: Mapping[str, Any] | None) -> dict[str, Any]:
    judge = judge or {}
    return {
        "path": cfg.judge_path,
        "receiver": "zai",
        "model": JUDGE_MODEL,
        "prompt": (
            "scripts.holmes_evals.judge (#666)"
            if cfg.judge_path == "opspilot"
            else "upstream autoevals LLMClassifier (classifiers.py)"
        ),
        "choice": judge.get("choice"),
        "passed": bool(judge.get("passed")) and judge.get("choice") == "A",
        "error_code": judge.get("error_code"),
        "attempts": judge.get("attempts"),
        "prompt_sha256": judge.get("prompt_sha256"),
    }


def build_summary(
    cfg: Config,
    qid: str,
    trial: int,
    attempts: Sequence[Mapping[str, Any]],
    chance_dir: Path,
) -> dict[str, Any]:
    """The evidence record: the last attempt's fields, no text, no secrets."""
    last = attempts[-1]
    history = [
        {"chance": i + 1, "category": a["category"], "code": a.get("code")}
        for i, a in enumerate(attempts)
    ]
    missing = last["category"] == "env_broken"
    harness = last.get("harness") or {}
    shift = _read_json(chance_dir / "shift.json")
    spec = LOG_QUESTIONS.get(qid)
    mode: str | None = None
    if spec is None:
        disclosures = list(DISCLOSURES)
    elif spec.backend in ("sql", "mongodb"):
        disclosures = list(C6_DISCLOSURES)
    elif qid in KNOWLEDGE_QUESTIONS:
        disclosures = list(C2_DISCLOSURES)
        mode = holmes_run_mode(cfg, qid)
        if mode == "cli" and not any(
            question_dir(cfg.upstream, qid).glob("*/SKILL.md")
        ):
            disclosures.append("HOLMES_NO_SKILL_CATALOG")
    else:
        disclosures = list(C5_DISCLOSURES)
        if isinstance(shift, Mapping) and shift.get("applied"):
            disclosures += LOKI_SHIFT_DISCLOSURES
        if spec.upstream_k8s_disabled:
            disclosures.append("K8S_TOOLS_UPSTREAM_DISABLED")
    if qid in R8_13_SKILL_ASSERTION_DROPPED:
        disclosures.append("SKILL_ASSERTION_DROPPED")
    if cfg.judge_path == "opspilot":
        disclosures.append("JUDGE_DIFFERS_FROM_UPSTREAM")
    block: dict[str, Any] = {
        "track": TRACK,
        "tool_face_note": TOOL_FACE_NOTE,
        "round": cfg.round,
        "qid": qid,
        "trial": trial,
        "chance": len(attempts),
        "upstream_commit": UPSTREAM_COMMIT,
        "model": MODEL,
        "model_env": dict(MODEL_ENV),
        "category": last["category"],
        "code": last.get("code"),
        "history": history,
    }
    if mode is not None:
        block["run_mode"] = mode
    # PR-E: upstream's harness turns TodoWrite on from the question's own
    # ``enable_todo``; the runner never forces it.
    block["todo"] = {
        "mode": "question",
        "enabled": _enable_todo(cfg, qid),
        "source": "question",
    }
    if not missing:
        block.update(
            {
                "judge": _judge_block(cfg, last.get("judge")),
                "seconds": last.get("seconds"),
                "wall_seconds": last.get("wall_seconds"),
                "tool_calls": last.get("tool_calls"),
                "llm_calls": last.get("llm_calls"),
                "pytest": {
                    "exit": last.get("pytest_exit"),
                    "timed_out": last.get("timed_out"),
                    "phases": harness.get("phases"),
                    "errors": harness.get("errors"),
                },
                "shift": shift if isinstance(shift, Mapping) else None,
                "question": {
                    "sha256_original": _sha256_file(
                        chance_dir / "question-original.txt"
                    ),
                    "sha256_delivered": _sha256_file(
                        chance_dir / "question-delivered.txt"
                    ),
                },
                "conversion": {
                    "text_sha256": _sha256_file(chance_dir / "judge-input.txt"),
                    "deshift_count": harness.get("deshift_count"),
                },
                "purged": last.get("purged"),
                "shift_day_wait_seconds": last.get("shift_day_wait_seconds"),
                "data_plane": _data_plane(cfg, qid),
                "disclosures": disclosures,
            }
        )
    return {
        "schema": COMPARE_SCHEMA,
        "status": "missing" if missing else "completed",
        "written_at": datetime.now(UTC).isoformat(),
        HOLMES_BLOCK: block,
    }


def _data_plane(cfg: Config, qid: str) -> dict[str, Any]:
    backend = backend_of(qid)
    spec = LOG_QUESTIONS.get(qid)
    if spec is None:
        url = cfg.secrets.get("ELASTICSEARCH_URL")
    elif spec.backend == "kubernetes":
        url = None
    elif spec.backend in ("sql", "mongodb"):
        from scripts.holmes_evals.db_backends import DB_QUESTIONS, product_url

        url = product_url(DB_QUESTIONS[qid])
    elif spec.address:
        url = f"http://localhost:{spec.address.rsplit(':', 1)[1]}"
    else:
        url = f"http://localhost:{cfg.victorialogs_port}"
    return {
        "backend": backend,
        "mode": "upstream-direct",
        "product_url": url,
        "victorialogs_image": VICTORIALOGS_IMAGE if backend == "victorialogs" else None,
    }


def holmes_run_mode(cfg: Config, qid: str) -> str:
    """The mode upstream's harness runs the question in: the question's own
    ``test_type``, else ``ASK_HOLMES_TEST_TYPE`` (this runner sets ``cli``)."""
    return (run_mode(cfg.upstream, qid) or "cli").lower()


def run_trial(cfg: Config, qid: str, trial: int) -> dict[str, Any]:
    trial_dir = cfg.trial_dir(qid, trial)
    attempts: list[dict[str, Any]] = []
    chance_dir = trial_dir
    for chance in range(1, CHANCES + 1):
        chance_dir = trial_dir / f"chance-{chance}"
        chance_dir.mkdir(parents=True, exist_ok=False)
        attempts.append(run_attempt(cfg, qid, chance_dir))
        if attempts[-1]["category"] != "env_broken":
            break
    summary = build_summary(cfg, qid, trial, attempts, chance_dir)
    path = cfg.summary_path(qid, trial)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".partial")
    partial.write_text(
        json.dumps(summary, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    )
    partial.replace(path)
    return summary


def run_round(cfg: Config) -> list[dict[str, Any]]:
    preflight(cfg)
    done = []
    for qid in cfg.questions:
        for trial in cfg.trials:
            if cfg.summary_path(qid, trial).is_file():
                continue  # resume: a written summary is final
            stale = cfg.trial_dir(qid, trial)
            if stale.exists():
                stale.rename(stale.with_name(f"{stale.name}.stale-{int(time.time())}"))
            summary = run_trial(cfg, qid, trial)
            block = summary[HOLMES_BLOCK]
            print(
                json.dumps(
                    {
                        "qid": qid,
                        "trial": trial,
                        "status": summary["status"],
                        "category": block["category"],
                        "code": block.get("code"),
                    }
                ),
                flush=True,
            )
            done.append(summary)
    return done


def scores(evidence_dir: Path, questions: Sequence[str]) -> dict[str, Any]:
    rows = load_rows(evidence_dir, "holmes")
    result = score([r.as_scoring() for r in rows], list(questions))
    result["track"] = TRACK
    result["tool_face_note"] = TOOL_FACE_NOTE
    return result


def _score_questions(spec: str | None) -> tuple[str, ...]:
    if not spec:
        return B1_QUESTIONS
    from scripts.holmes_evals.driver import resolve_questions

    return resolve_questions(spec)


def _parse_trials(spec: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in spec.split(",") if x.strip())
    except ValueError:
        raise RoundAbort("BAD_TRIALS") from None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("round")
    r.add_argument("--round", required=True)
    r.add_argument("--upstream", type=Path, required=True)
    r.add_argument("--holmes-python", type=Path, required=True)
    r.add_argument("--env-file", type=Path, required=True)
    r.add_argument("--judge-path", choices=JUDGE_PATHS, default=DEFAULT_JUDGE_PATH)
    r.add_argument("--questions")
    r.add_argument("--trials", default="1,2,3")
    r.add_argument("--kubeconfig", type=Path)
    r.add_argument("--victorialogs-port", type=int, default=DEFAULT_VICTORIALOGS_PORT)
    r.add_argument("--tmp-root", type=Path)
    r.add_argument("--evidence-root", type=Path)
    s = sub.add_parser("scores")
    s.add_argument("--round", required=True)
    s.add_argument("--evidence-root", type=Path, default=ROOT / "docs" / "evidence")
    s.add_argument("--out", type=Path)
    s.add_argument("--questions", help="numbers, names or groups (b1, c5); default B1")
    args = parser.parse_args(argv)
    try:
        if args.command == "scores":
            evidence = args.evidence_root / f"holmes-compare-{args.round}"
            out = args.out or evidence / "scores.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(
                    scores(evidence, _score_questions(args.questions)),
                    ensure_ascii=False,
                    indent=1,
                )
                + "\n"
            )
            return 0
        from scripts.holmes_evals.driver import resolve_questions

        cfg = Config(
            round=args.round,
            upstream=args.upstream.resolve(),
            holmes_python=args.holmes_python.absolute(),
            judge_path=args.judge_path,
            questions=resolve_questions(args.questions),
            trials=_parse_trials(args.trials),
            secrets=load_env_file(args.env_file),
            kubeconfig=args.kubeconfig.resolve() if args.kubeconfig else None,
            victorialogs_port=args.victorialogs_port,
        )
        if args.tmp_root:
            cfg.tmp_root = args.tmp_root.resolve()
        if args.evidence_root:
            cfg.evidence_root = args.evidence_root.resolve()
        run_round(cfg)
    except RoundAbort as exc:
        out = {"status": "aborted", "code": exc.code}
        if exc.hint:
            out["hint"] = exc.hint
        print(json.dumps(out), flush=True)
        return 2
    except Exception as exc:  # noqa: BLE001 - resolve_questions raises driver's RoundAbort
        code = getattr(exc, "code", None)
        if not isinstance(code, str):
            raise
        print(json.dumps({"status": "aborted", "code": code}), flush=True)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
