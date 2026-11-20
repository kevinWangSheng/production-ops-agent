"""#667 PR-B stand-in end-to-end tests for the agent entry SREGym launches.

Written from docs/tasks/2026-11-19-sregym-667.md "PR-B 公开接口" (including the
Loki read-only gateway revision) and pre-review contract-test groups 3 and 5,
without reading the PR-B implementation.

The conductor is a local HTTP stand-in (PR-A ``fake_conductor``); every host
action goes through a recording ``EntryHost``. Its members follow the shapes of
#666's ``Backends`` (``start_pg(dir, port) -> PgInstance``, ``start_web`` /
``start_worker`` / ``start_port_forward -> Started``, ``submit ->
SubmitResult``, ``run_state(dsn, run_id) -> str | None``, ``collect(dsn,
run_id) -> (run block, ledger)``); the stand-ins accept any arguments and only
rely on returning those types. Time is a fixed clock moved only by ``sleep``.

Assertions are on what is observable: requests the conductor received, the
environments handed to web and worker, the question and idempotency key sent
to the product, port-forward targets, the exit code and the entry record.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.sregym_eval_support import LITE_17, mod

PID = LITE_17[2]  # network_policy_block: the canary
SQUASHED_PID = PID.replace("_", "")
ADMIN_KUBECONFIG = "/home/eng/.kube/sregym-admin-from-main.yaml"
ENGINEER_KUBECONFIG_NAME = "engineer-sregym.kubeconfig"
ENGINEER_TOKEN = "eng-admin-token-c0ffee"
JUDGE_KEY = "zai-judge-key-deadbeef"
READONLY_TOKEN = "sa-readonly-token-41"
DEEPSEEK = "sk-deepseek-test-77"
LANGSMITH = "lsv2-langsmith-test-88"
LOKI_USER = "reader"
LOKI_PASS = "gw-reader-pass-99"
GATEWAY_URL = "http://127.0.0.1:29532"
RUN_ID = "11111111-2222-3333-4444-555555555555"
INC_ID = "66666666-7777-8888-9999-000000000000"
START = datetime(2026, 11, 19, 18, 0, tzinfo=UTC)
MONO0 = 5000.0

APP = {
    "app_name": "Social Network",
    "namespace": "social-network",
    "namespaces": ["social-network"],
    "descriptions": "A social network with unidirectional follow relationships.",
    "stages": ["diagnosis"],
}


def _report(summary: str) -> dict[str, Any]:
    return {
        "schema_version": "m0-report-v2",
        "assessment_status": "completed",
        "conclusion": "supported",
        "summary": summary,
        "claims": [
            {
                "kind": "fact",
                "text": "compose-post-service returned connection refused to user-service",
                "evidence_ids": ["ev-1"],
                "target_refs": ["target-1"],
                "time_scope_ref": "window-1",
            }
        ],
        "gaps": [],
        "next_steps": ["inspect the network policies in social-network"],
    }


REPORT_SUMMARY = "user-service is unreachable from compose-post-service since 18:03Z."


class Conductor(mod("fake_conductor").FakeConductor):  # type: ignore[misc]
    """Records the harness clock when a ``POST /submit`` arrives."""

    harness: Harness | None = None

    def handle(self, method: str, path: str, body: bytes) -> tuple[int, Any]:
        if method == "POST" and path == "/submit" and self.harness is not None:
            self.harness.submit_at.append(self.harness.mono)
            self.harness.events.append("conductor_submit")
        return super().handle(method, path, body)


def _env_of(args: tuple, kwargs: dict) -> dict[str, str]:
    if isinstance(kwargs.get("env"), Mapping):
        return dict(kwargs["env"])
    for a in args:
        if isinstance(a, Mapping) and all(isinstance(k, str) for k in a):
            if any(k in a for k in ("PATH", "HOME")) or any(
                k.startswith("OPSPILOT_") for k in a
            ):
                return dict(a)
    raise AssertionError(f"no environment among {args!r} {kwargs!r}")


class Harness:
    def __init__(self, tmp_path: Path, app: Mapping[str, Any] = APP) -> None:
        self.tmp = tmp_path
        self.mono = MONO0
        self.events: list[str] = []
        self.submit_at: list[float] = []
        self.sleeps: list[float] = []
        self.forwards: list[str] = []
        self.forwards_raw: list[str] = []
        self.forward_fail: set[str] = set()
        self.web_envs: list[dict[str, str]] = []
        self.worker_envs: list[dict[str, str]] = []
        self.intake_calls: list[dict[str, Any]] = []
        self.intake_status = 201
        self.intake_code: str | None = None
        self.states: list[str | None] = ["completed"]
        self.report_available = True
        self.web_fails = False
        self.conductor_urls: list[str] = []
        self.on_poll: Any = None
        self.stopped: list[str] = []
        self.fake = Conductor(app, stages=tuple(app["stages"]))
        self.fake.harness = self
        self.server = mod("fake_conductor").serve(self.fake)

    # -- conductor -------------------------------------------------------------------------

    def conductor(self, base_url: str, *a: Any, **k: Any) -> Any:
        self.conductor_urls.append(base_url)
        port = self.server.server_address[1]
        return mod("conductor").ConductorClient(f"http://127.0.0.1:{port}")

    # -- time --------------------------------------------------------------------------------

    def now(self) -> datetime:
        return START + timedelta(seconds=self.mono - MONO0)

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.mono += max(float(s), 0.0)
        if self.fake.stage == "tearing_down":
            self.fake.advance("done")

    # -- services ----------------------------------------------------------------------------

    def _started(self, name: str) -> Any:
        services = mod_services()
        return services.Started(name, lambda: self.stopped.append(name))

    def start_port_forward(self, *args: Any, **kwargs: Any) -> Any:
        raw = json.dumps([list(args), kwargs], default=str)
        # the test's own tmp path holds the test name; keep it out of matching
        text = raw.replace(str(self.tmp), "<tmp>")
        self.forwards.append(text)
        self.forwards_raw.append(raw)
        self.events.append("port_forward")
        for name in self.forward_fail:
            if name in text:
                raise mod_services().ServiceError("NOT_READY", name)
        return self._started("forward")

    def start_pg(self, *args: Any, **kwargs: Any) -> Any:
        self.events.append("start_pg")
        pg = _pg_module()
        return pg.PgInstance(
            directory=self.tmp / "pg",
            port=29510,
            owner_dsn="postgresql://owner@127.0.0.1:29510/db",
            runtime_dsn="postgresql://worker@127.0.0.1:29510/db",
            web_dsn="postgresql://web@127.0.0.1:29510/db",
        )

    def stop_pg(self, *args: Any, **kwargs: Any) -> None:
        self.events.append("stop_pg")

    def start_web(self, *args: Any, **kwargs: Any) -> Any:
        self.web_envs.append(_env_of(args, kwargs))
        self.events.append("start_web")
        if self.web_fails:
            raise mod_services().ServiceError("PROCESS_EXITED", "web")
        return self._started("web")

    def start_worker(self, *args: Any, **kwargs: Any) -> Any:
        self.worker_envs.append(_env_of(args, kwargs))
        self.events.append("start_worker")
        return self._started("worker")

    def submit(self, *args: Any, **kwargs: Any) -> Any:
        self.intake_calls.append(
            {"args": [str(a) for a in args], **{k: str(v) for k, v in kwargs.items()}}
        )
        self.events.append("intake")
        ok = self.intake_status == 201
        return mod_intake().SubmitResult(
            status=self.intake_status,
            incident_id=INC_ID if ok else None,
            run_id=RUN_ID if ok else None,
            replayed=False if ok else None,
            code=self.intake_code,
        )

    def run_state(self, *args: Any, **kwargs: Any) -> str | None:
        self.events.append("run_state")
        if self.on_poll is not None:
            self.on_poll()
        if len(self.states) > 1:
            return self.states.pop(0)
        return self.states[0]

    def collect(self, *args: Any, **kwargs: Any) -> tuple[dict, dict]:
        self.events.append("collect")
        block = {
            "run_id": RUN_ID,
            "incident_id": INC_ID,
            "run": {"state": "completed", "bound_target_id": "m0-otel-20260909"},
            "report": _report(REPORT_SUMMARY),
            "report_present": True,
            "steps": [],
            "outcome": {
                "available": True,
                "run_id": RUN_ID,
                "report_available": self.report_available,
            },
        }
        return block, {"run": {"run_id": RUN_ID}, "steps": []}

    # -- wiring --------------------------------------------------------------------------------

    def entry_host(self) -> Any:
        cls = mod("agent_entry").EntryHost
        given = {
            f.name: getattr(self, f.name)
            for f in dataclasses.fields(cls)
            if hasattr(self, f.name)
        }
        return cls(**given)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def mod_services() -> Any:
    import importlib

    return importlib.import_module("scripts.itbench_lite.services")


def mod_intake() -> Any:
    import importlib

    return importlib.import_module("scripts.holmes_evals.intake")


def _pg_module() -> Any:
    import importlib

    return importlib.import_module("scripts.eval_live_lab")


# --- fixtures -------------------------------------------------------------------------------------


def _private(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o600)
    return path


@dataclasses.dataclass
class Setup:
    harness: Harness
    job: Any
    job_path: Path
    environ: dict[str, str]
    engineer_kubeconfig: Path
    work_dir: Path


def _setup(tmp_path: Path, *, app: Mapping[str, Any] = APP, **job_over: Any) -> Setup:
    h = Harness(tmp_path, app)

    work = tmp_path / "work"
    work.mkdir()
    eng = _private(
        tmp_path / ENGINEER_KUBECONFIG_NAME,
        f"apiVersion: v1\nkind: Config\nusers:\n- name: a\n  user:\n    token: {ENGINEER_TOKEN}\n",
    )
    ca = _private(
        tmp_path / "k8s-ca.crt",
        "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",
    )
    target = _private(
        tmp_path / "k8s-target.json",
        json.dumps(
            {
                "api_url": "https://127.0.0.1:26444",
                "ca_file": str(ca),
                "token": READONLY_TOKEN,
            }
        ),
    )
    secrets = _private(
        tmp_path / "secrets.env",
        f"DEEPSEEK_API_KEY={DEEPSEEK}\nLANGSMITH_API_KEY={LANGSMITH}\n",
    )
    loki = _private(
        tmp_path / "loki-readonly.env",
        f"OPSPILOT_LOKI_URL={GATEWAY_URL}\nOPSPILOT_LOKI_USERNAME={LOKI_USER}\n"
        f"OPSPILOT_LOKI_PASSWORD={LOKI_PASS}\n",
    )
    fields: dict[str, Any] = {
        "round": "r1",
        "seq": 3,
        "trial": 2,
        "work_dir": str(work),
        "evidence_hint": None,
        "ports": mod("ports").Ports(),
        "cluster_path": "filter_proxy_readonly",
        "k8s_target_file": str(target),
        "engineer_kubeconfig": str(eng),
        "secrets_file": str(secrets),
        "loki_env_file": str(loki),
        "trace": "off",
        "project_name": "opspilot-sregym-r1",
        "problem_digest": mod("boundary").problem_id_digest(PID),
    }
    fields.update(job_over)
    job = mod("job").Job(**fields)
    job_path = tmp_path / "job.json"
    mod("job").write_job(job_path, job)

    # What SREGym's main.py hands its non-container agent: its whole environment.
    environ = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "SREGYM_EVAL_JOB": str(job_path),
        "KUBECONFIG": ADMIN_KUBECONFIG,
        "JUDGE_API_KEY": JUDGE_KEY,
        "JUDGE_API_BASE": "https://api.z.ai/api/paas/v4",
        "ZAI_API_KEY": JUDGE_KEY,
        "SREGYM_AGENT_REGISTRY": str(tmp_path / "agents.yaml"),
        "SREGYM_PROBLEM_ID": PID,
        "PROBLEM_ID": PID,
        "API_HOSTNAME": "0.0.0.0",
        "API_PORT": "28000",
        "MCP_SERVER_PORT": "29954",
        "MCP_SERVER_URL": "http://127.0.0.1:29954",
        "K8S_PROXY_PORT": "26443",
        "KIND_CLUSTER_NAME": "sregym",
        "AGENT_LOGS_DIR": str(tmp_path / "agent-logs"),
        "ENGINEER_TOKEN": ENGINEER_TOKEN,
    }
    return Setup(h, job, job_path, environ, eng, work)


@pytest.fixture
def make(tmp_path: Path):  # noqa: ANN201
    made: list[Setup] = []

    def _make(**kw: Any) -> Setup:
        s = _setup(tmp_path, **kw)
        made.append(s)
        return s

    yield _make
    for s in made:
        s.harness.server.shutdown()
        s.harness.server.server_close()


def _run(s: Setup) -> int:
    return mod("agent_entry").main(environ=dict(s.environ), host=s.harness.entry_host())


def _submits(s: Setup) -> list[Any]:
    return [
        r.body
        for r in s.harness.fake.requests
        if r.method == "POST" and r.path == "/submit"
    ]


def _record(s: Setup) -> dict[str, Any]:
    return json.loads((s.work_dir / "entry-record.json").read_text())


def _product_visible(s: Setup) -> list[str]:
    h = s.harness
    out: list[str] = []
    for env in h.web_envs + h.worker_envs:
        out.extend(env.keys())
        out.extend(env.values())
    for call in h.intake_calls:
        out.extend(call["args"])
        out.extend(str(v) for k, v in call.items() if k != "args")
    return out


def _squash(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch not in "-_ \t\r\n")


# --- the happy path -----------------------------------------------------------------------------


def test_happy_path_submits_the_report_once_and_exits_zero(make) -> None:  # noqa: ANN001
    s = make()
    s.harness.fake.advance("diagnosis")
    assert _run(s) == 0

    subs = _submits(s)
    assert len(subs) == 1
    assert set(subs[0]) == {"stage", "solution"}
    assert subs[0]["stage"] == "diagnosis"
    assert REPORT_SUMMARY in subs[0]["solution"]
    assert all(
        not (isinstance(r.body, dict) and r.body.get("stage") == "mitigation")
        for r in s.harness.fake.requests
    )
    assert s.harness.fake.stage == "done"
    assert s.harness.conductor_urls and all(
        u.rstrip("/") == "http://127.0.0.1:28000" for u in s.harness.conductor_urls
    )

    rec = _record(s)
    assert rec["exit_code"] == 0
    assert rec["leak_check"] == "ok"
    assert rec["idempotency_key"] == "sregym-r1-p03-t2"
    assert rec["run_id"] == RUN_ID
    sub = rec["submission"]
    assert (
        sub["submitted"] is True and sub["empty"] is False and sub["status_code"] == 200
    )
    assert set(rec["app"]) <= {"app_name", "namespaces"}
    assert rec["app"]["app_name"] == APP["app_name"]


def test_question_is_the_pr_a_rendering_and_is_kept_beside_the_record(make) -> None:  # noqa: ANN001
    s = make()
    s.harness.fake.advance("diagnosis")
    assert _run(s) == 0
    c = mod("conductor")
    expected = mod("question").render_question(
        c.AppInfo(
            app_name=APP["app_name"],
            namespace=APP["namespace"],
            namespaces=tuple(APP["namespaces"]),
            descriptions=APP["descriptions"],
            stages=tuple(APP["stages"]),
        )
    )
    assert len(s.harness.intake_calls) == 1
    call = s.harness.intake_calls[0]
    assert call.get("question") == expected or expected in call["args"]
    assert call.get("key") == "sregym-r1-p03-t2" or "sregym-r1-p03-t2" in call["args"]
    assert (s.work_dir / "question.txt").read_text() == expected
    assert (
        _record(s)["question_sha256"] == hashlib.sha256(expected.encode()).hexdigest()
    )


def test_flow_order_asks_after_services_and_submits_after_collect(make) -> None:  # noqa: ANN001
    s = make()
    s.harness.fake.advance("diagnosis")
    assert _run(s) == 0
    ev = s.harness.events
    assert ev.index("port_forward") < ev.index("start_web")
    assert ev.index("start_pg") < ev.index("start_web")
    assert ev.index("start_web") < ev.index("intake")
    assert ev.index("start_worker") < ev.index("intake") or ev.index(
        "start_worker"
    ) < ev.index("run_state")
    assert ev.index("intake") < ev.index("conductor_submit")
    assert ev.index("collect") < ev.index("conductor_submit")


# --- group 5: environment isolation ---------------------------------------------------------------


def test_web_and_worker_get_none_of_mains_environment(make) -> None:  # noqa: ANN001
    s = make()
    s.harness.fake.advance("diagnosis")
    assert _run(s) == 0
    h = s.harness
    assert len(h.web_envs) == 1 and len(h.worker_envs) == 1
    forbidden_values = (
        ADMIN_KUBECONFIG,
        JUDGE_KEY,
        ENGINEER_TOKEN,
        str(s.engineer_kubeconfig),
    )
    for role, env in (("web", h.web_envs[0]), ("worker", h.worker_envs[0])):
        for key in env:
            assert not key.startswith(
                (
                    "KUBECONFIG",
                    "JUDGE_",
                    "SREGYM_",
                    "MCP_SERVER_",
                    "API_HOSTNAME",
                    "API_PORT",
                    "K8S_PROXY_",
                    "AGENT_",
                    "HARNESS_",
                    "KIND_",
                )
            ), (role, key)
            assert key not in (
                "ZAI_API_KEY",
                "GLM_API_KEY",
                "PROBLEM_ID",
                "ENGINEER_TOKEN",
            ), (role, key)
        for key, value in env.items():
            for bad in forbidden_values:
                assert bad not in value, (role, key)
    web = h.web_envs[0]
    for key in (
        "OPSPILOT_K8S_TOKEN",
        "DEEPSEEK_API_KEY",
        "OPSPILOT_LOKI_BEARER_TOKEN",
        "LANGSMITH_API_KEY",
        "OPSPILOT_LOKI_USERNAME",
        "OPSPILOT_LOKI_PASSWORD",
    ):
        assert key not in web, key
    for value in web.values():
        for secret in (READONLY_TOKEN, DEEPSEEK, LANGSMITH, LOKI_PASS):
            assert secret not in value


def test_worker_env_carries_the_product_settings(make) -> None:  # noqa: ANN001
    s = make()
    s.harness.fake.advance("diagnosis")
    assert _run(s) == 0
    web, worker = s.harness.web_envs[0], s.harness.worker_envs[0]
    assert worker["OPSPILOT_TOOL_PROFILE"] == "otel-demo-ext"
    assert worker["OPSPILOT_WORKER_CAPACITY"] == "1"
    assert worker["OPSPILOT_K8S_NAMESPACE"] == "social-network"
    assert worker["OPSPILOT_K8S_TOKEN"] == READONLY_TOKEN
    assert worker["DEEPSEEK_API_KEY"] == DEEPSEEK
    for env in (web, worker):
        assert env["OPSPILOT_LOKI_URL"].rstrip("/") == GATEWAY_URL
        assert "OPSPILOT_LOKI_BEARER_TOKEN" not in env
    assert worker["OPSPILOT_LOKI_USERNAME"] == LOKI_USER
    assert worker["OPSPILOT_LOKI_PASSWORD"] == LOKI_PASS
    joined = " ".join(worker.values())
    assert "127.0.0.1:29590" in joined  # prometheus forward
    assert "127.0.0.1:29586" in joined  # jaeger forward
    assert "127.0.0.1:29531" not in joined  # never the Loki forward itself


def test_problem_id_canary_is_not_visible_to_the_product(make) -> None:  # noqa: ANN001
    s = make()
    s.harness.fake.advance("diagnosis")
    assert _run(s) == 0
    for value in _product_visible(s):
        assert SQUASHED_PID not in _squash(value)
    assert SQUASHED_PID not in _squash((s.work_dir / "entry-record.json").read_text())
    assert SQUASHED_PID not in _squash(s.job_path.read_text())


def test_problem_id_in_app_description_stops_before_asking(make) -> None:  # noqa: ANN001
    app = dict(APP, descriptions="Scenario: Network Policy Block on the social graph.")
    s = make(app=app)
    s.harness.fake.advance("diagnosis")
    code = _run(s)
    assert code != 0
    assert s.harness.intake_calls == []
    for value in _product_visible(s):
        assert SQUASHED_PID not in _squash(value)
    rec = _record(s)
    assert rec["exit_code"] == code
    assert rec.get("leak_check") != "ok"


# --- port-forwards and the observability fallbacks --------------------------------------------------


def test_port_forwards_use_the_engineer_kubeconfig_in_observe(make) -> None:  # noqa: ANN001
    s = make()
    s.harness.fake.advance("diagnosis")
    assert _run(s) == 0
    fw = s.harness.forwards
    # Kafka wiring interface: three observe forwards plus svc/kafka in the app namespace
    assert len(fw) == 4
    for svc, mapping in (
        ("prometheus-server", "29590"),
        ("jaeger-out", "29586"),
        ("loki", "29531"),
    ):
        hits = [f for f in fw if svc in f and mapping in f]
        assert len(hits) == 1, svc
        assert "observe" in hits[0], svc
    (kafka,) = [f for f in fw if "kafka" in f]
    assert "observe" not in kafka
    assert APP["namespaces"][0] in kafka and "9092" in kafka
    for raw in s.harness.forwards_raw:
        assert ADMIN_KUBECONFIG not in raw
        assert str(s.engineer_kubeconfig) in raw


def test_prometheus_forward_failure_points_at_unreachable(make) -> None:  # noqa: ANN001
    s = make()
    s.harness.forward_fail = {"prometheus-server"}
    s.harness.fake.advance("diagnosis")
    assert _run(s) == 0
    assert _record(s)["observability"]["prometheus"] == "missing"
    joined = " ".join(s.harness.worker_envs[0].values())
    assert "127.0.0.1:29599" in joined
    assert "127.0.0.1:29590" not in joined
    assert len(_submits(s)) == 1


def test_loki_forward_failure_keeps_the_gateway(make) -> None:  # noqa: ANN001
    s = make()
    s.harness.forward_fail = {"loki"}
    s.harness.fake.advance("diagnosis")
    assert _run(s) == 0
    assert _record(s)["observability"]["loki"] == "missing"
    assert s.harness.worker_envs[0]["OPSPILOT_LOKI_URL"].rstrip("/") == GATEWAY_URL


def test_multi_namespace_uses_the_first_and_discloses(make) -> None:  # noqa: ANN001
    app = dict(APP, namespaces=["social-network", "social-network-db"])
    s = make(app=app)
    s.harness.fake.advance("diagnosis")
    assert _run(s) == 0
    assert s.harness.worker_envs[0]["OPSPILOT_K8S_NAMESPACE"] == "social-network"
    assert "K8S_NAMESPACE_FIRST_OF_MANY" in _record(s)["disclosures"]


# --- no report, refused intake, deadline ---------------------------------------------------------------


def test_no_report_submits_an_empty_string_once(make) -> None:  # noqa: ANN001
    s = make()
    s.harness.report_available = False
    s.harness.fake.advance("diagnosis")
    assert _run(s) == 0
    assert _submits(s) == [{"stage": "diagnosis", "solution": ""}]
    rec = _record(s)
    assert rec["conversion_reason"] == "REPORT_REJECTED_BY_PRODUCT"
    assert rec["submission"]["empty"] is True
    assert rec["submission"]["submitted"] is True


def test_refused_intake_is_not_retried_and_submits_empty(make) -> None:  # noqa: ANN001
    s = make()
    s.harness.intake_status = 422
    s.harness.intake_code = "QUESTION_INVALID"
    s.harness.fake.advance("diagnosis")
    assert _run(s) == 0
    assert len(s.harness.intake_calls) == 1
    assert _submits(s) == [{"stage": "diagnosis", "solution": ""}]
    assert _record(s)["intake"]["code"] == "QUESTION_INVALID"


@pytest.mark.parametrize("remaining", [1800.0, 600.0])
def test_deadline_is_remaining_seconds_minus_margin(make, remaining: float) -> None:  # noqa: ANN001
    s = make()
    s.harness.states = ["running"]
    s.harness.fake.remaining_s = remaining
    s.harness.fake.advance("diagnosis")
    assert _run(s) == 0
    assert _submits(s) == [{"stage": "diagnosis", "solution": ""}]
    deadline = MONO0 + remaining - 120
    (at,) = s.harness.submit_at
    assert deadline <= at <= deadline + 30, at - MONO0
    assert "worker" in s.harness.stopped
    assert _record(s)["run_wait"]["timed_out"] is True


def test_run_finishing_before_deadline_is_submitted_without_waiting_for_it(
    make,
) -> None:  # noqa: ANN001
    s = make()
    s.harness.states = ["running", "running", "completed"]
    s.harness.fake.advance("diagnosis")
    assert _run(s) == 0
    (at,) = s.harness.submit_at
    assert at < MONO0 + 1800 - 120
    sub = _submits(s)[0]
    assert REPORT_SUMMARY in sub["solution"]


# --- conductor refusals and exit codes -------------------------------------------------------------------


@pytest.mark.parametrize(
    "status, code", [(409, "SUBMIT_REJECTED"), (503, "SUBMIT_UNAVAILABLE")]
)
def test_refused_submission_is_not_counted_as_submitted(
    make, status: int, code: str
) -> None:  # noqa: ANN001
    s = make()
    h = s.harness
    h.fake.advance("diagnosis")
    armed = {"done": False}

    def arm() -> None:
        if not armed["done"]:
            armed["done"] = True
            h.fake.fail_next(status)

    # arm on the first collect: the next conductor request is the submission
    original = h.collect

    def collect(*a: Any, **k: Any) -> tuple[dict, dict]:
        out = original(*a, **k)
        arm()
        return out

    h.collect = collect  # type: ignore[method-assign]
    assert _run(s) == 5
    assert len(_submits(s)) == 1
    sub = _record(s)["submission"]
    assert sub["submitted"] is False
    assert sub["error_code"] == code
    assert _record(s)["exit_code"] == 5


def test_invalid_job_exits_2_without_submitting(make) -> None:  # noqa: ANN001
    s = make()
    s.job_path.write_text("{not json")
    s.harness.fake.advance("diagnosis")
    assert _run(s) == 2
    assert _submits(s) == []
    assert s.harness.intake_calls == []


@pytest.mark.parametrize("stage", ["tearing_down", "done"])
def test_stage_already_passed_exits_3(make, stage: str) -> None:  # noqa: ANN001
    s = make()
    s.harness.fake.advance(stage)
    assert _run(s) == 3
    assert _submits(s) == []
    assert s.harness.intake_calls == []
    assert _record(s)["exit_code"] == 3


def test_diagnosis_never_reached_exits_3(make) -> None:  # noqa: ANN001
    s = make()  # stays in setup; the fixed clock passes diagnosis_wait_s
    assert _run(s) == 3
    assert _submits(s) == []
    assert _record(s)["exit_code"] == 3


def test_aborted_exits_6(make) -> None:  # noqa: ANN001
    s = make()
    s.harness.fake.advance("aborted")
    assert _run(s) == 6
    assert _submits(s) == []
    assert _record(s)["exit_code"] == 6


def test_product_start_failure_still_submits_empty_and_exits_4(make) -> None:  # noqa: ANN001
    s = make()
    s.harness.web_fails = True
    s.harness.fake.advance("diagnosis")
    assert _run(s) == 4
    assert _submits(s) == [{"stage": "diagnosis", "solution": ""}]
    assert s.harness.intake_calls == []
    assert _record(s)["exit_code"] == 4


# --- clarifications (interface 0045f5ec and lead notes) ----------------------------------------------


@pytest.mark.parametrize("which", ["secrets_file", "loki_env_file"])
def test_unreadable_secret_files_exit_4_and_submit_empty(make, which: str) -> None:  # noqa: ANN001
    s = make()
    Path(getattr(s.job, which)).unlink()
    s.harness.fake.advance("diagnosis")
    assert _run(s) == 4
    assert _submits(s) == [{"stage": "diagnosis", "solution": ""}]
    rec = _record(s)
    assert rec["exit_code"] == 4
    assert "ENTRY_FILES_UNREADABLE" in json.dumps(rec)


def test_unexpected_crash_exits_4_and_submits_empty(make) -> None:  # noqa: ANN001
    s = make()

    def boom(*a: Any, **k: Any) -> Any:
        raise KeyError("unexpected")

    s.harness.start_pg = boom  # type: ignore[method-assign]
    s.harness.fake.advance("diagnosis")
    assert _run(s) == 4
    assert _submits(s) == [{"stage": "diagnosis", "solution": ""}]
    rec = _record(s)
    assert rec["exit_code"] == 4
    assert "ENTRY_CRASHED:KeyError" in json.dumps(rec)


def test_unknown_remaining_seconds_counts_as_1800(make) -> None:  # noqa: ANN001
    s = make()
    s.harness.states = ["running"]
    s.harness.fake.remaining_s = None
    s.harness.fake.advance("diagnosis")
    assert _run(s) == 0
    (at,) = s.harness.submit_at
    deadline = MONO0 + 1800 - 120
    assert deadline <= at <= deadline + 30
