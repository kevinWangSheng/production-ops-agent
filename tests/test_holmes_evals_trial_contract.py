"""#666 PR-B contract: one trial end to end with replaced Backends (H-4, H-5,
H-7, H-8, H-10, H-12, H-13, H-20..H-24, R10-1, H-U1).

Independent contract tests written from the task record's PR-B public
interface, including the signature additions (``run_trial``,
``run_question``, ``finish_judging``, ``prepare_round``/``Round``, every
``Backends`` field, the per-trial files), without reading the
implementation. Everything that touches the host is a fake: PG, web and
worker are recorders, the model is never called, the judge is a recorder that
writes a ``JudgeResult``. Elasticsearch is the in-process fake ES served on
127.0.0.1 (``start_fake_es``), so setup, purge and shift run for real against
it. The upstream checkout is synthetic (answer isolation): its
``verify_upstream`` is patched out, nothing reads the real question set.

Covered: attempt order (cleanup + purge before ``before_test``, shift before
delivery, worker stopped before ``collect``, services stopped in reverse
order), environments handed to each process, the delivered question (R10-1
rewrite; verbatim with ``OPSPILOT_CLUSTER_NAME`` for a multi-cluster question),
deshifted judge text with the tool-call attachment, the summary's
``holmes_eval`` block and what it must not contain, the trial files and
marker, ``trial_state``/``trial_record`` on the result, the void line with
retries, ``no_report``, a failed judge process and ``finish_judging``,
deterministic errors in ``run_question``, resume, and the 280 midnight wait.

Assumptions beyond the interface (also marked ``# ASSUMPTION:``):

* ``driver.Round()`` takes no arguments; ``prepare_round`` reads the upstream
  through ``questions.verify_upstream`` (patched here, and ``driver``'s own
  reference if it imported the name).
* ``report_object`` accepts a run block with ``report`` (a ReportV2 mapping)
  and ``outcome.report_available`` as ``run_freeze.collect`` produces it;
  the fake ``collect`` returns only the keys the summary needs.
* ``run_state`` returning ``"completed"`` (a product terminal state) ends the
  wait.
* Summary sub-blocks are checked for the documented keys and for values the
  fakes fix; exact counts/hashes only where the interface defines them
  (``text_sha256`` = sha256 of ``judge-input.txt`` as UTF-8).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import socket
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.itbench_lite.services import Started
from tests.holmes_evals_b_support import (
    ENGINEER,
    Es,
    encoded,
    new_store,
    no_proxy_env,  # noqa: F401 - fixture
)
from tests.holmes_evals_support import FIXTURES, mod, write_question

pytestmark = pytest.mark.usefixtures("no_proxy_env")

MODEL = "model-fake-value-4"
JUDGE = "judge-fake-value-5"
SHELL_MODEL = "shell-model-fake-9"
SHELL_ES = "shell-es-fake-10"
RUN = "22222222-2222-4222-8222-222222222222"
INC = "11111111-1111-4111-8111-111111111111"
EXPECTED_SENTINEL = "EXPECTED-SENTINEL the index uses severity"
RATIONALE_SENTINEL = "RATIONALE-SENTINEL judged from the answer"
TOOLOUT_SENTINEL = "TOOLOUT-SENTINEL refused connection"
Q275 = "275_es_field_name_correction"
Q262 = "262_ambiguous_cluster_reference"
Q185 = "185_elasticsearch_cross_region_search"
Q280 = "280_es_multi_quirk"
PROMPT_275 = (
    "Between 2026-05-14T12:00:00Z and 2026-05-14T12:03:00Z checkout-275 logged "
    "errors; it stopped at 12:02. How many ERROR lines were there?"
)
PROMPT_262 = "Are there errors for checkout in the production cluster logs?"
SUMMARY_KEYS = {
    "schema", "track", "round", "qid", "upstream_commit", "trial", "chance",
    "git", "ports", "question", "cluster_name", "setup", "shift", "delivery",
    "run_wait", "conversion", "judge", "category", "time_box_limited",
    "disclosures", "history",
}  # fmt: skip
DISCLOSURES = {
    "TOOL_FACE_FIXED", "NO_INTERMEDIATE_OUTPUT", "DESHIFT_TIME_ONLY_UNCHANGED",
    "DESHIFT_DATE_MIDPOINT", "QUESTION_TIME_REWRITTEN", "CLUSTER_NAME_ENV_ONLY",
    "SKILL_ASSERTION_DROPPED", "JUDGE_DIFFERS_FROM_UPSTREAM",
}  # fmt: skip


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _report(summary: str) -> dict[str, Any]:
    return {
        "schema_version": "m0-report-v2",
        "assessment_status": "completed",
        "conclusion": "supported",
        "summary": summary,
        "claims": [
            {
                "kind": "fact",
                "text": "checkout-275 logged ERROR lines with field severity",
                "evidence_ids": ["ev-opaque-1"],
                "target_refs": ["target-1"],
                "time_scope_ref": "window-1",
            }
        ],
        "gaps": [],
        "next_steps": ["check the payment pod"],
    }


class Harness:
    """Fake host. ``events`` records the order of everything the driver did."""

    def __init__(self, tmp_path: Path, start: datetime) -> None:
        self.tmp = tmp_path
        self.clock = start
        self.events: list[str] = []
        self.store = new_store()
        self.es = Es(self.store)
        self._served: Any = None
        self.es_url: str | None = None
        self.readonly_key: str | None = None
        self.scripts: list[dict[str, Any]] = []
        self.web_envs: list[dict[str, str]] = []
        self.worker_envs: list[dict[str, str]] = []
        self.submits: list[dict[str, Any]] = []
        self.judge_jobs: list[dict[str, Any]] = []
        self.judge_texts: list[str] = []
        self.sleeps: list[float] = []
        self.passwords: list[str] = []
        self.started = {"pg": 0, "web": 0, "worker": 0}
        self.stopped = {"pg": 0, "web": 0, "worker": 0}
        # Behaviour switches for individual tests.
        self.seed: dict[str, list[tuple[str, dict, list[dict]]]] = {}
        self.worker_delay = 0.0
        self.report_available = True
        self.judge_mode = "ok"  # or "fail" (exit 1, no result file)
        self.tool_calls_read = 0

    # -- clock
    def now(self) -> datetime:
        self.clock += timedelta(milliseconds=250)
        return self.clock

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.events.append(f"sleep:{float(seconds)}")
        self.clock += timedelta(seconds=float(seconds))

    # -- fake ES
    def start_fake_es(self, port: int, work_dir: Path) -> tuple[Started, str]:
        self.events.append("start_fake_es")
        server = mod("fake_es").serve(self.store, "127.0.0.1", port)
        host, bound = server.server_address[:2]
        self.es_url = f"http://{host}:{bound}"
        _ensure_serving(server, self.es_url)

        def stop() -> None:
            self.events.append("stop_fake_es")
            server.shutdown()
            server.server_close()

        return Started(name="fake-es", _stop=stop, info={"url": self.es_url}), ENGINEER

    def readonly_identity(self, es_url: str, engineer_key: str, out: Path) -> dict:
        assert engineer_key == ENGINEER
        self.events.append("readonly_identity")
        minted = self.es.ok("POST", "/_security/api_key", {"name": "ro"})
        self.readonly_key = minted["encoded"]
        assert self.readonly_key == encoded(minted["id"], minted["api_key"])
        return {"OPSPILOT_ES_URL": es_url, "OPSPILOT_ES_API_KEY": self.readonly_key}

    def unreachable_k8s(self, work_dir: Path, port: int) -> Any:
        ca = Path(work_dir) / "fake-ca.pem"
        ca.parent.mkdir(parents=True, exist_ok=True)
        ca.write_text("not a real ca\n")
        return mod("driver").K8sTarget(
            api_url=f"https://127.0.0.1:{port}",
            ca_file=ca,
            token="k8s-fake-value-6",
            namespace="otel-demo",
        )

    def es_admin(self, url: str, engineer_key: str) -> Any:
        self.events.append("es_admin")
        assert engineer_key == ENGINEER
        return mod("shift").EsAdmin(url, engineer_key)

    # -- setup scripts
    def run_script(self, script, *, operation, cwd, env, timeout_s, log_path):
        indices = sorted(
            r["index"]
            for r in self.es.ok(
                "GET", "/_cat/indices", query={"format": "json", "h": "index"}
            )
        )
        self.scripts.append(
            {
                "operation": operation,
                "script": script,
                "cwd": Path(cwd),
                "env": dict(env),
                "timeout_s": timeout_s,
                "indices_before": indices,
            }
        )
        self.events.append(f"script:{operation}")
        started = self.now()
        if operation == "before_test":
            qid = Path(cwd).name
            for index, props, docs in self.seed.get(qid, []):
                self.es.create(index, props)
                if docs:
                    self.es.bulk(index, docs)
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(f"{operation} ok\n")
        ended = self.now()
        return mod("setup").ScriptResult(
            operation=operation,
            exit_code=0,
            timed_out=False,
            started_at=started,
            ended_at=ended,
            seconds=(ended - started).total_seconds(),
            log_sha256=hashlib.sha256(Path(log_path).read_bytes()).hexdigest(),
            skipped=not script,
        )

    # -- services
    def _started(self, name: str, info: dict | None = None) -> Started:
        self.started[name] += 1

        def stop() -> None:
            self.stopped[name] += 1
            self.events.append(f"stop:{name}")

        return Started(name=name, _stop=stop, info=info or {"pid": 0})

    def start_pg(self, directory: Path, port: int) -> Any:
        self.started["pg"] += 1
        self.events.append("start:pg")
        return SimpleNamespace(
            owner_dsn=f"postgresql://owner:pw@127.0.0.1:{port}/opspilot",
            runtime_dsn=f"postgresql://eval_worker:pw@127.0.0.1:{port}/opspilot",
            web_dsn=f"postgresql://eval_web:pw@127.0.0.1:{port}/opspilot",
            port=port,
            directory=directory,
        )

    def stop_pg(self, instance: Any) -> None:
        self.stopped["pg"] += 1
        self.events.append("stop:pg")

    def start_web(self, env: Mapping[str, str], port: int, log_path: Path) -> Started:
        self.events.append("start:web")
        self.web_envs.append(dict(env))
        return self._started("web")

    def start_worker(
        self, env: Mapping[str, str], log_path: Path, *, settle_seconds: float
    ) -> Started:
        self.events.append("start:worker")
        self.worker_envs.append(dict(env))
        self.clock += timedelta(seconds=self.worker_delay)
        return self._started("worker")

    def port_in_use(self, port: int) -> bool:
        return False

    # -- the Run
    def submit(self, web_url, account, *, question, key, target_id):
        self.events.append("submit")
        self.passwords.append(account.password)
        self.submits.append(
            {
                "web_url": web_url,
                "question": question,
                "key": key,
                "target_id": target_id,
            }
        )
        return mod("intake").SubmitResult(
            status=201, incident_id=INC, run_id=RUN, replayed=False, code=None
        )

    def run_state(self, dsn: str, run_id: str) -> str | None:
        self.events.append("run_state")
        return "completed"

    def _shifted_error_ts(self) -> str | None:
        for name in self.es.ok(
            "GET", "/_cat/indices", query={"format": "json", "h": "index"}
        ):
            out = self.es.search(
                name["index"], {"query": {"term": {"severity": "ERROR"}}}
            )
            for hit in out["hits"]["hits"]:
                return hit["_source"].get("@timestamp")
        return None

    def collect(self, dsn: str, run_id: str) -> tuple[dict, dict]:
        self.events.append("collect")
        ts = self._shifted_error_ts() or "2026-11-18T09:00:00Z"
        report = _report(f"checkout-275 logged 10 ERROR lines; first at {ts}.")
        block = {
            "run_id": run_id,
            "incident_id": INC,
            "run": {
                "state": "completed",
                "question": "q",
                "bound_target_id": "m0-otel-20260909",
            },
            "incident": {"incident_id": INC},
            "incident_runs": [],
            "versions": {"prompt_revision": "p1"},
            "report": report,
            "report_present": True,
            "conclusion_step": None,
            "steps": [],
            "tokens": {},
            "tool_requests": [],
            "observations": [],
            "investigation_evidence": {},
            "cited_evidence": [],
            "audit": {},
            "audit_undated": [],
            "alert_delivery_outcomes": [],
            "alert_deliveries": [],
            "outcome": {
                "available": True,
                "run_id": run_id,
                "report_available": self.report_available,
            },
            "collected_at_db": "2026-11-18T10:00:00+00:00",
        }
        return block, {"run": {"run_id": run_id}, "steps": []}

    def read_tool_calls(self, dsn: str, run_id: str) -> list:
        self.events.append("read_tool_calls")
        self.tool_calls_read += 1
        ts = self._shifted_error_ts() or "2026-11-18T09:00:00Z"
        text = mod("text")
        return [
            text.ToolCall(
                tool="elasticsearch_search",
                params={"index": "app-275-*", "query": "severity:ERROR"},
                output={"hits": [{"@timestamp": ts, "message": TOOLOUT_SENTINEL}]},
            ),
            text.ToolCall(
                tool="kubernetes_pods",
                params={},
                refused_code="timeout",
                refused_reason="SOURCE_TIMEOUT",
            ),
        ]

    def trace_roots(self, project, run_id, env) -> dict:
        return {}

    def judge(self, job: Mapping[str, Any]) -> int:
        self.events.append("judge")
        self.judge_jobs.append(dict(job))
        self.judge_texts.append(Path(job["text_file"]).read_text(encoding="utf-8"))
        if self.judge_mode == "fail":
            return 1
        rationale = Path(job["rationale_dir"])
        rationale.mkdir(parents=True, exist_ok=True)
        (rationale / "rationale.json").write_text(
            json.dumps({"rationale": RATIONALE_SENTINEL})
        )
        Path(job["out"]).parent.mkdir(parents=True, exist_ok=True)
        Path(job["out"]).write_text(
            json.dumps(
                {
                    "choice": "A",
                    "passed": True,
                    "error_code": None,
                    "attempts": 1,
                    "prompt_sha256": mod("judge").HOLMES_EVAL_PROMPT_SHA256,
                    "request_bytes": 1234,
                }
            )
        )
        return 0

    def backends(self) -> Any:
        return mod("driver").Backends(
            start_pg=self.start_pg,
            stop_pg=self.stop_pg,
            start_web=self.start_web,
            start_worker=self.start_worker,
            run_script=self.run_script,
            es_admin=self.es_admin,
            submit=self.submit,
            run_state=self.run_state,
            collect=self.collect,
            read_tool_calls=self.read_tool_calls,
            trace_roots=self.trace_roots,
            judge=self.judge,
            port_in_use=self.port_in_use,
            start_fake_es=self.start_fake_es,
            readonly_identity=self.readonly_identity,
            unreachable_k8s=self.unreachable_k8s,
            now=self.now,
            sleep=self.sleep,
        )

    def secrets(self) -> list[str]:
        values = [ENGINEER, MODEL, JUDGE, SHELL_MODEL, SHELL_ES, *self.passwords]
        if self.readonly_key:
            values.append(self.readonly_key)
        return values


def _ensure_serving(server: Any, url: str) -> None:
    import threading

    from tests.holmes_evals_b_support import _answers

    if not _answers(url + "/"):
        threading.Thread(target=server.serve_forever, daemon=True).start()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A synthetic upstream, a prepared round and the harness."""
    questions, driver = mod("questions"), mod("driver")
    upstream = tmp_path / "upstream"
    write_question(
        upstream,
        Q275,
        {
            "user_prompt": PROMPT_275,
            "before_test": "echo seed",
            "after_test": "echo keep  # reentrant: does not delete",
            "include_tool_calls": True,
            "expected_output": [EXPECTED_SENTINEL],
        },
    )
    write_question(
        upstream,
        Q262,
        {
            "user_prompt": PROMPT_262,
            "before_test": "echo seed",
            "after_test": "echo clean",
            "cluster_name": "production",
            "expected_output": [EXPECTED_SENTINEL],
        },
    )
    write_question(
        upstream,
        Q185,
        {"user_prompt": "Search 2024-01-05 geo logs.", "before_test": "echo seed",
         "after_test": "echo clean", "expected_output": [EXPECTED_SENTINEL]},
    )  # fmt: skip
    write_question(
        upstream,
        Q280,
        {"user_prompt": "Errors on 2026-05-14T12:00:00Z?", "before_test": "echo seed",
         "after_test": "echo clean", "expected_output": [EXPECTED_SENTINEL]},
    )  # fmt: skip
    # ASSUMPTION: the upstream pin is checked through verify_upstream.
    monkeypatch.setattr(questions, "verify_upstream", lambda root: None)
    if hasattr(driver, "verify_upstream"):
        monkeypatch.setattr(driver, "verify_upstream", lambda root: None)
    monkeypatch.setenv("OPSPILOT_LEDGER_DIR", str(tmp_path / "ledgers"))
    monkeypatch.setenv("ZAI_API_KEY", JUDGE)
    monkeypatch.setenv("DEEPSEEK_API_KEY", SHELL_MODEL)
    monkeypatch.setenv("ELASTICSEARCH_API_KEY", SHELL_ES)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    env_file = tmp_path / "secrets.env"
    env_file.write_text(f"DEEPSEEK_API_KEY={MODEL}\n")
    env_file.chmod(0o600)

    def make(
        start: datetime, qids: tuple[str, ...] = (Q275,)
    ) -> tuple[Harness, Any, Any]:
        h = Harness(tmp_path, start)
        ports = dataclasses.replace(driver.Ports(), es=_free_port())
        wanted = {
            "round": "t1",
            "upstream": upstream,
            "questions": qids,
            "ports": ports,
            "es_mode": "fake",
            "env_file": env_file,
            "trace": "off",
            "judge": True,
            "allow_dirty": True,
            "tmp_root": tmp_path / "tmp",
            "evidence_root": tmp_path / "evidence",
            "poll_seconds": 0.0,
        }
        names = {f.name for f in dataclasses.fields(driver.Config)}
        cfg = driver.Config(**{k: v for k, v in wanted.items() if k in names})
        round_ = driver.Round()  # ASSUMPTION: no arguments
        driver.prepare_round(cfg, h.backends(), round_)
        made.append(round_)
        return h, cfg, round_

    made: list[Any] = []
    yield make
    for round_ in made:
        round_.close()


START = datetime(2026, 11, 18, 10, 0, 0, tzinfo=UTC)


def _seed_275(h: Harness) -> None:
    props = {
        "@timestamp": {"type": "date"},
        "severity": {"type": "keyword"},
        "message": {"type": "text"},
    }
    docs = [
        {
            "@timestamp": "2026-05-14T12:00:01Z",
            "severity": "ERROR",
            "message": "refused 1",
        },
        {"@timestamp": "2026-05-14T12:01:30Z", "severity": "WARN", "message": "slow"},
        {"@timestamp": "2026-05-14T12:02:15Z", "severity": "INFO", "message": "ok"},
    ]
    h.seed[Q275] = [("app-275-logs-x", props, docs)]
    # Leftover of an earlier attempt and another question's index.
    h.es.create("app-275-old", {"m": {"type": "keyword"}})
    h.es.create("app-276-keep", {"m": {"type": "keyword"}})


def _trial_dir(cfg: Any, qid: str, n: int) -> Path:
    return Path(cfg.tmp_root) / cfg.round / qid / f"trial-{n}"


def _summary(cfg: Any, qid: str, n: int) -> tuple[dict, str]:
    path = Path(cfg.evidence_dir) / qid / f"trial-{n}" / "summary.json"
    raw = path.read_text(encoding="utf-8")
    return json.loads(raw), raw


def _load(cfg: Any, qid: str) -> Any:
    return mod("questions").load_question(Path(cfg.upstream), qid)


def _no_secrets(h: Harness, *texts: str) -> None:
    for text in texts:
        for secret in h.secrets():
            assert secret not in text, secret[:6]


# -- the fixed-time question, fully --------------------------------------------------


def test_fixed_time_trial_end_to_end(world) -> None:
    driver, shift_mod = mod("driver"), mod("shift")
    h, cfg, _ = world(START)
    _seed_275(h)
    record = driver.run_trial(cfg, h.backends(), Q275, _load(cfg, Q275), 1)
    assert record["status"] == "completed"
    assert str(record["run_id"]) == RUN

    # Order of the attempt.
    ev = h.events
    first = lambda name: ev.index(name)  # noqa: E731
    ops = [s["operation"] for s in h.scripts]
    assert ops[:2] == ["after_test", "before_test"]
    assert ops[-1] == "after_test" and len(ops) == 3
    before = next(s for s in h.scripts if s["operation"] == "before_test")
    assert "app-275-old" not in before["indices_before"]  # purged first (H-7)
    assert "app-276-keep" in before["indices_before"]
    assert first("script:before_test") < first("start:worker") < first("submit")
    assert first("submit") < first("run_state") < first("collect")
    assert ev.index("stop:worker") < first("collect")
    assert first("collect") < first("read_tool_calls") < first("judge")
    cleanup_after = len(ev) - 1 - ev[::-1].index("script:after_test")
    assert cleanup_after > first("collect")
    stops = [e for e in ev if e in ("stop:worker", "stop:web", "stop:pg")]
    assert stops == ["stop:worker", "stop:web", "stop:pg"]
    assert h.started == h.stopped == {"pg": 1, "web": 1, "worker": 1}

    # Setup processes: engineer key, question directory, default timeout.
    for s in h.scripts:
        assert s["env"]["ELASTICSEARCH_API_KEY"] == ENGINEER
        assert s["env"]["ELASTICSEARCH_URL"] == h.es_url
        assert s["env"]["ES_UPDATE_SHARD_LIMIT"] == "true"
        assert s["cwd"] == Path(cfg.upstream) / FIXTURES / Q275
        assert s["timeout_s"] == 300
        assert not [k for k in s["env"] if k.startswith("OPSPILOT_")]
        assert MODEL not in json.dumps(s["env"]) and JUDGE not in json.dumps(s["env"])

    # Web and worker: built from blank, engineer key nowhere.
    (web,) = h.web_envs
    (worker,) = h.worker_envs
    for env in (web, worker):
        assert not [k for k in env if k.startswith("ELASTICSEARCH_")]
        assert not [k for k, v in env.items() if ENGINEER in v or SHELL_ES in v]
        assert JUDGE not in " ".join(env.values())
    for name in ("OPSPILOT_ES_API_KEY", "DEEPSEEK_API_KEY", "ZAI_API_KEY"):
        assert name not in web
    assert worker["OPSPILOT_ES_API_KEY"] == h.readonly_key
    assert worker["DEEPSEEK_API_KEY"] == MODEL
    assert worker["OPSPILOT_WORKER_CAPACITY"] == "1"
    assert "OPSPILOT_CLUSTER_NAME" not in worker

    # Data shifted before delivery, by the recorded offset.
    summary, raw = _summary(cfg, Q275, 1)
    he = summary["holmes_eval"]
    seconds = he["shift"]["seconds"]
    assert he["shift"]["applied"] is True
    assert seconds > 0 and seconds % 60 == 0
    (sub,) = h.submits
    expected_question, _ = shift_mod.rewrite_question(PROMPT_275, seconds)
    assert sub["question"] == expected_question
    assert "stopped at 12:02" in sub["question"]
    assert sub["target_id"] == mod("intake").TARGET_ID
    assert sub["key"] in {
        f"holmes-t1-{Q275}-t1-c1",
        "holmes-t1-275-t1-c1",
    }
    assert sub["web_url"].rstrip("/") == f"http://127.0.0.1:{cfg.ports.web}"

    # Judge job: narrowed env, text deshifted, tool calls attached.
    (job,) = h.judge_jobs
    assert job["qid"] == Q275
    assert Path(job["upstream"]) == Path(cfg.upstream)
    assert job["env"]["ZAI_API_KEY"] == JUDGE
    assert job["env"].get("PYTHON_DOTENV_DISABLED") == "1"
    for name in ("DEEPSEEK_API_KEY", "ELASTICSEARCH_API_KEY", "OPSPILOT_ES_API_KEY"):
        assert name not in job["env"]
    trial_dir = _trial_dir(cfg, Q275, 1)
    assert Path(job["text_file"]) == trial_dir / "judge-input.txt"
    assert Path(job["rationale_dir"]).is_relative_to(trial_dir / "answer-derived")
    (text,) = h.judge_texts
    assert "first at 2026-05-14T12:00:01Z" in text
    assert "# Tool Calls" in text and TOOLOUT_SENTINEL in text
    assert "REFUSED timeout" in text
    shifted = datetime(2026, 5, 14, 12, 0, 1, tzinfo=UTC) + timedelta(seconds=seconds)
    assert shifted.strftime("%Y-%m-%dT%H:%M:%S") not in text

    # Summary block.
    assert summary["status"] == "completed"
    assert "run" in summary and "trace" in summary
    assert SUMMARY_KEYS <= set(he)
    assert he["schema"] == "holmes-eval-trial-1"
    assert (he["qid"], he["trial"], he["chance"], he["round"]) == (Q275, 1, 1, "t1")
    assert he["upstream_commit"] == mod("questions").UPSTREAM_COMMIT
    assert he["question"]["sha256_original"] == _sha(PROMPT_275)
    assert he["question"]["sha256_delivered"] == _sha(expected_question)
    assert he["question"]["rewrites"]
    assert he["question"]["time_only_phrases"] == 1
    assert he["cluster_name"]["value"] is None
    assert he["cluster_name"]["env_set"] is False
    setup = he["setup"]
    assert {"cleanup_before", "before_test", "cleanup_after", "purged"} <= set(setup)
    assert setup["before_test"]["operation"] == "before_test"
    assert setup["before_test"]["exit_code"] == 0
    assert "app-275-old" in json.dumps(setup["purged"])
    assert he["delivery"]["run_id"] == RUN
    assert he["delivery"]["incident_id"] == INC
    assert 0 <= he["delivery"]["setup_to_delivery_seconds"] <= 120
    conv = he["conversion"]
    assert conv["with_tool_calls"] is True
    assert conv["tool_calls"] == 2
    assert conv["deshift_count"] >= 2
    assert conv["reason"] is None
    assert conv["text_sha256"] == _sha(text)
    judge = he["judge"]
    assert judge["receiver"] == "zai"
    assert judge["mode"] == "strict"
    assert (judge["choice"], judge["passed"], judge["error_code"]) == ("A", True, None)
    assert judge["prompt_sha256"] == mod("judge").HOLMES_EVAL_PROMPT_SHA256
    assert judge["request_bytes"] == 1234
    assert he["category"] == "ok"
    assert he["time_box_limited"] is False
    assert set(he["disclosures"]) <= DISCLOSURES
    assert {"QUESTION_TIME_REWRITTEN", "SKILL_ASSERTION_DROPPED"} <= set(
        he["disclosures"]
    )

    # What the summary must never hold.
    for forbidden in (RATIONALE_SENTINEL, EXPECTED_SENTINEL, TOOLOUT_SENTINEL, text):
        assert forbidden not in raw
    _no_secrets(h, raw)

    # Files and resume state.
    marker = json.loads((trial_dir / "complete.json").read_text())
    assert marker["status"] == "completed" and marker["category"] == "ok"
    assert (trial_dir / "judge-result.json").is_file()
    assert driver.trial_state(cfg, Q275, 1) == "done"
    trial = driver.trial_record(summary)
    assert (trial.qid, trial.index, trial.passed, trial.category) == (
        Q275,
        1,
        True,
        "ok",
    )
    _no_secrets(h, (trial_dir / "judge-result.json").read_text(), marker.__repr__())

    # Cleanup after: this question's indices are gone, others stay.
    names = {
        r["index"]
        for r in h.es.ok("GET", "/_cat/indices", query={"format": "json", "h": "index"})
    }
    assert not [n for n in names if n.startswith("app-275-")]
    assert "app-276-keep" in names


# -- a relative multi-cluster question ------------------------------------------------


def test_multi_cluster_relative_question_verbatim_with_cluster_env(world) -> None:
    driver = mod("driver")
    h, cfg, _ = world(START, (Q262,))
    recent = (START - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    h.seed[Q262] = [
        ("app-262-prod-logs", {"timestamp": {"type": "date"}, "severity": {"type": "keyword"}},
         [{"timestamp": recent, "severity": "ERROR"}]),
    ]  # fmt: skip
    record = driver.run_trial(cfg, h.backends(), Q262, _load(cfg, Q262), 1)
    assert record["status"] == "completed"
    (sub,) = h.submits
    assert sub["question"] == PROMPT_262  # verbatim: no cluster prefix (H-U1)
    (worker,) = h.worker_envs
    assert worker["OPSPILOT_CLUSTER_NAME"] == "production"
    assert h.tool_calls_read == 0  # include_tool_calls not set
    summary, raw = _summary(cfg, Q262, 1)
    he = summary["holmes_eval"]
    assert he["cluster_name"]["value"] == "production"
    assert he["cluster_name"]["env_set"] is True
    assert he["shift"]["applied"] is False
    assert he["shift"]["reason"] == "RELATIVE_TIME"
    assert he["question"]["sha256_delivered"] == he["question"]["sha256_original"]
    assert he["conversion"]["with_tool_calls"] is False
    assert "# Tool Calls" not in h.judge_texts[0]
    _no_secrets(h, raw)


# -- void line ---------------------------------------------------------------------------


def test_void_line_retries_then_missing_without_delivery(world) -> None:
    driver = mod("driver")
    h, cfg, _ = world(START)
    _seed_275(h)
    h.worker_delay = 130.0  # setup end -> delivery always > 120 s
    record = driver.run_trial(cfg, h.backends(), Q275, _load(cfg, Q275), 1)
    assert record["status"] == "missing"
    assert h.submits == []
    assert h.judge_jobs == []
    assert h.started["worker"] == 3  # three chances
    assert h.started == h.stopped
    summary, raw = _summary(cfg, Q275, 1)
    assert summary["status"] == "missing"
    assert summary["reason"] == "CHANCES_EXHAUSTED"
    he = summary["holmes_eval"]
    assert he["qid"] == Q275 and he["trial"] == 1
    assert len(he["history"]) == 3
    assert all("SETUP_TO_DELIVERY_EXCEEDED" in json.dumps(e) for e in he["history"])
    assert driver.trial_state(cfg, Q275, 1) == "missing"
    trial = driver.trial_record(summary)
    assert (trial.category, trial.passed) == ("env_broken", False)
    _no_secrets(h, raw)


# -- no report ---------------------------------------------------------------------------


def test_report_rejected_by_product_is_not_judged(world) -> None:
    driver = mod("driver")
    h, cfg, _ = world(START)
    _seed_275(h)
    h.report_available = False
    record = driver.run_trial(cfg, h.backends(), Q275, _load(cfg, Q275), 1)
    assert record["status"] == "completed"
    assert h.judge_jobs == []
    summary, _ = _summary(cfg, Q275, 1)
    he = summary["holmes_eval"]
    assert he["conversion"]["reason"] == "REPORT_REJECTED_BY_PRODUCT"
    assert he["category"] == "no_report"
    assert he["judge"]["choice"] is None
    marker = json.loads((_trial_dir(cfg, Q275, 1) / "complete.json").read_text())
    assert marker["category"] == "no_report"
    assert driver.trial_state(cfg, Q275, 1) == "done"
    assert driver.trial_record(summary).category == "no_report"


# -- judge process failure and finish_judging ------------------------------------------


def test_failed_judge_process_is_resumable(world) -> None:
    driver = mod("driver")
    h, cfg, _ = world(START)
    _seed_275(h)
    h.judge_mode = "fail"
    record = driver.run_trial(cfg, h.backends(), Q275, _load(cfg, Q275), 1)
    assert record["status"] == "completed"
    summary, _ = _summary(cfg, Q275, 1)
    assert summary["holmes_eval"]["judge"]["error_code"] == "JUDGE_PROCESS_FAILED"
    assert summary["holmes_eval"]["judge"]["choice"] is None
    trial_dir = _trial_dir(cfg, Q275, 1)
    assert not (trial_dir / "complete.json").exists()
    assert (trial_dir / "judge-input.txt").is_file()
    assert driver.trial_state(cfg, Q275, 1) == "judge"
    submits = len(h.submits)

    h.judge_mode = "ok"
    out = driver.finish_judging(cfg, h.backends(), Q275, 1)
    assert out["choice"] == "A" and out["category"] == "ok"
    assert len(h.submits) == submits  # judged again, not re-run
    summary, _ = _summary(cfg, Q275, 1)
    assert summary["holmes_eval"]["judge"]["choice"] == "A"
    assert summary["holmes_eval"]["category"] == "ok"
    assert driver.trial_state(cfg, Q275, 1) == "done"
    assert h.judge_texts[0] == h.judge_texts[1]


# -- run_question: deterministic errors and resume --------------------------------------


def test_deterministic_shift_error_marks_remaining_trials_missing(world) -> None:
    driver = mod("driver")
    h, cfg, _ = world(START, (Q185,))
    old = (START - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = (START - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    h.seed[Q185] = [
        ("app-185-geo-us-a", {"@timestamp": {"type": "date"}},
         [{"@timestamp": old}, {"@timestamp": recent}]),
    ]  # fmt: skip
    result = driver.run_question(cfg, h.backends(), Q185, [1, 2, 3])
    assert result["qid"] == Q185
    assert h.submits == []
    for n in (1, 2, 3):
        summary, raw = _summary(cfg, Q185, n)
        assert summary["status"] == "missing"
        assert summary["reason"] == "SHIFT_OVERLAP"
        assert driver.trial_state(cfg, Q185, n) == "missing"
        _no_secrets(h, raw)
    trials = [driver.trial_record(_summary(cfg, Q185, n)[0]) for n in (1, 2, 3)]
    assert {t.category for t in trials} == {"env_broken"}
    assert all(t.time_box_limited for t in trials)
    # Only the first trial tried; the rest were not attempted again.
    assert len([s for s in h.scripts if s["operation"] == "before_test"]) == 1


def test_resume_skips_done_and_sets_leftovers_aside(world) -> None:
    driver = mod("driver")
    h, cfg, _ = world(START)
    _seed_275(h)
    driver.run_trial(cfg, h.backends(), Q275, _load(cfg, Q275), 1)
    assert driver.trial_state(cfg, Q275, 1) == "done"
    leftover = _trial_dir(cfg, Q275, 2) / "chance-1" / "web.log"
    leftover.parent.mkdir(parents=True)
    leftover.write_text("interrupted\n")
    submits = len(h.submits)

    result = driver.run_question(cfg, h.backends(), Q275, [1, 2])
    assert 1 in result["skipped"]
    assert len(h.submits) == submits + 1
    assert h.submits[-1]["key"].endswith("-t2-c1")
    assert driver.trial_state(cfg, Q275, 2) == "done"
    stale = list((_trial_dir(cfg, Q275, 2) / "stale").rglob("web.log"))
    assert [p.read_text() for p in stale] == ["interrupted\n"]


# -- 280 waits for UTC midnight -----------------------------------------------------------


def test_280_waits_past_utc_midnight_before_setup(world) -> None:
    driver = mod("driver")
    h, cfg, _ = world(datetime(2026, 11, 18, 23, 55, 0, tzinfo=UTC), (Q280,))
    h.seed[Q280] = [
        ("app-280-a-x", {"@timestamp": {"type": "date"}, "severity": {"type": "keyword"}},
         [{"@timestamp": "2026-05-14T12:00:00Z", "severity": "ERROR"}]),
    ]  # fmt: skip
    driver.run_trial(cfg, h.backends(), Q280, _load(cfg, Q280), 1)
    first_script = next(i for i, e in enumerate(h.events) if e.startswith("script:"))
    waited = sum(
        float(e.split(":", 1)[1])
        for e in h.events[:first_script]
        if e.startswith("sleep:")
    )
    assert waited >= 300  # until 5 s past 00:00 UTC (H-24)
    assert any(s["operation"] == "before_test" for s in h.scripts)
