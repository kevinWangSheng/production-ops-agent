"""#667 PR-B contract tests: one pass of the round driver with a stand-in host.

Written from docs/tasks/2026-11-19-sregym-667.md "PR-B 公开接口" including
"签名补充（回答测试作者）" (Config, ResultsLock, run_trial / prepare_round,
Host signatures, probe_product_path, recheck_leak), without reading the PR-B
implementation. Pre-review groups 4 and 7, S-4 (fresh token and can-i every
pass), S-5 layer 2, S-14, S-U2 (filter proxy or direct, probe both).

``Host.run_main`` stands in for SREGym's ``main.py``: it reads the job the
driver wrote, writes the entry record the entry would have written and, when a
test asks, a result CSV in a new run directory. No cluster, no subprocess.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import stat
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.sregym_eval_support import LITE_17, code_of, mod

PID = LITE_17[2]  # network_policy_block, seq 3
SEQ = 3
SQUASHED_PID = PID.replace("_", "")
CA_PEM = b"-----BEGIN CERTIFICATE-----\nMIIBfakeCA\n-----END CERTIFICATE-----\n"
JUDGE_KEY = "judge-canary-value"
DEEPSEEK = "deepseek-canary-value"
LANGSMITH = "langsmith-canary-value"
START = datetime(2026, 11, 19, 18, 0, tzinfo=UTC)


def _round_abort() -> type[BaseException]:
    import importlib

    return importlib.import_module("scripts.itbench_lite.driver").RoundAbort


def _started(name: str, stopped: list[str]) -> Any:
    import importlib

    services = importlib.import_module("scripts.itbench_lite.services")
    return services.Started(name, lambda: stopped.append(name))


def _kubeconfig(context: str = "kind-sregym") -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": context,
        "clusters": [
            {
                "name": context,
                "cluster": {
                    "server": "https://127.0.0.1:43211",
                    "certificate-authority-data": base64.b64encode(CA_PEM).decode(),
                },
            }
        ],
        "contexts": [
            {"name": context, "context": {"cluster": context, "user": context}}
        ],
        "users": [{"name": context, "user": {"token": "engineer-admin-token"}}],
    }


def _entry_record(work: Path, question: str, key: str) -> dict[str, Any]:
    return {
        "exit_code": 0,
        "stage_seen": "diagnosis",
        "app": {"app_name": "Social Network", "namespaces": ["social-network"]},
        "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
        "question_file": str(work / "question.txt"),
        "idempotency_key": key,
        "intake": {"status": 201, "code": None},
        "run_id": None,
        "run_wait": None,
        "conversion_reason": "REPORT_REJECTED_BY_PRODUCT",
        "submission": {
            "submitted": True,
            "empty": True,
            "status_code": 200,
            "error_code": None,
            "text_sha256": None,
            "chars": 0,
        },
        "observability": {"prometheus": "ok", "jaeger": "ok", "loki": "ok"},
        "disclosures": [],
        "leak_check": "ok",
    }


def _result_row() -> dict[str, str]:
    return {
        "attempt": "1",
        "Diagnosis.success": "True",
        "Diagnosis.accuracy": "0.8",
        "Diagnosis.composite_score": "0.75",
        "Diagnosis.dimensions": repr([{"id": "D1", "name": "x", "score": 1.0}]),
        "Diagnosis.unscored_reason": "",
        "Diagnosis.oracles": "",
        "timed_out": "False",
        "deploy_failed": "False",
        "deploy_attempts": "1",
        "run_status": "complete",
        "incomplete_reason": "",
        "TTL": "312.5",
    }


def _write_csv(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = _result_row()
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=sorted(row))
        w.writeheader()
        w.writerow(row)


def _hosts(tmp: Path) -> Path:
    """A temp hosts file with the Kafka advertised name (Kafka wiring interface)."""
    path = tmp / "hosts"
    path.write_text("127.0.0.1 kafka\n")
    return path


class DriverHost:
    def __init__(self, tmp: Path, upstream: Path) -> None:
        self.tmp = tmp
        self.upstream = upstream
        self.config = _kubeconfig()
        self.events: list[str] = []
        self.kubectl_calls: list[tuple[list[str], dict[str, str]]] = []
        self.can_i_override: dict[tuple[str, str], str] = {}
        self.probe_status = {"GET": 200, "POST": 403}
        self.probes: list[tuple[str, str, str, str]] = []
        self.main_calls: list[dict[str, Any]] = []
        self.main_exit = 0
        self.write_result_csv = False
        self.question = "Application: Social Network\nNamespace: social-network\n"
        self.busy_ports: set[int] = set()
        self.proxy_calls: list[tuple] = []
        self.stopped: list[str] = []
        self.token_n = 0
        self.clock = START

    # kubectl -------------------------------------------------------------------------------

    def kubectl(
        self, argv: Sequence[str], env: Mapping[str, str], timeout: float
    ) -> subprocess.CompletedProcess:
        argv = list(argv)
        self.kubectl_calls.append((argv, dict(env)))
        out = ""
        if "current-context" in argv:
            out = self.config["current-context"] + "\n"
        elif "config" in argv and "view" in argv:
            out = json.dumps(self.config)
        elif "create" in argv and "token" in argv:
            self.token_n += 1
            out = f"sa-token-{self.token_n}\n"
            self.events.append("token")
        elif "can-i" in argv:
            self.events.append("can-i")
            verb, res = argv[3], argv[4]
            sub = [a.split("=", 1)[1] for a in argv if a.startswith("--subresource=")]
            resource = f"{res}/{sub[0]}" if sub else res
            expect = "no"
            for c in mod("boundary").CAN_I_CHECKS:
                if (c.verb, c.resource) == (verb, resource):
                    expect = c.expect
            out = self.can_i_override.get((verb, resource), expect) + "\n"
        return subprocess.CompletedProcess(argv, 0, out, "")

    # filter proxy, probe ------------------------------------------------------------------------

    def start_filter_proxy(
        self,
        host: Any,
        upstream: Path,
        readonly_kubeconfig: Path,
        port: int,
        work_dir: Path,
    ) -> tuple[Any, Any]:
        self.events.append("filter_proxy")
        self.proxy_calls.append((upstream, readonly_kubeconfig, port, work_dir))
        ca = Path(work_dir) / "proxy-ca.crt"
        ca.write_bytes(CA_PEM)
        target = mod("identity").K8sTarget(
            api_url=f"https://127.0.0.1:{port}", ca_file=str(ca), token="proxy-token"
        )
        return _started("filter_proxy", self.stopped), target

    def http_probe(
        self,
        method: str,
        url: str,
        token: str,
        ca_file: str,
        body: bytes | None = None,
    ) -> int:
        self.events.append(f"probe:{method}")
        self.probes.append((method, url, token, ca_file))
        return self.probe_status[method]

    def start_gateway(
        self, backend: str, port: int, upstream: str, state_dir: Path, out: Path
    ) -> tuple[Any, dict[str, str]]:
        self.events.append("gateway")
        self.gateway = (backend, port, upstream)
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(
            f"OPSPILOT_LOKI_URL=http://127.0.0.1:{port}\n"
            "OPSPILOT_LOKI_USERNAME=reader\nOPSPILOT_LOKI_PASSWORD=gw-pass\n"
        )
        Path(out).chmod(0o600)
        return _started("gateway", self.stopped), {"url": f"http://127.0.0.1:{port}"}

    def spawn(self, *a: Any, **k: Any) -> Any:  # pragma: no cover
        raise AssertionError("spawn is only for the real start_filter_proxy")

    # main.py --------------------------------------------------------------------------------------

    def run_main(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        log_path: Path,
        timeout: float,
    ) -> int:
        self.events.append("run_main")
        job_path = Path(env["SREGYM_EVAL_JOB"])
        job_text = job_path.read_text()
        self.main_calls.append(
            {
                "argv": list(argv),
                "cwd": Path(cwd),
                "env": dict(env),
                "job_text": job_text,
                "job_mode": stat.S_IMODE(job_path.stat().st_mode),
            }
        )
        job = json.loads(job_text)
        work = Path(job["work_dir"])
        work.mkdir(parents=True, exist_ok=True)
        key = mod("question").idempotency_key(job["round"], job["seq"], job["trial"])
        (work / "question.txt").write_text(self.question)
        (work / "entry-record.json").write_text(
            json.dumps(_entry_record(work, self.question, key))
        )
        if self.write_result_csv:
            run_dir = self.upstream / "results" / f"run-{len(self.main_calls):03d}"
            _write_csv(run_dir / "opspilot" / PID / f"{PID}_opspilot_results.csv")
        return self.main_exit

    # misc ---------------------------------------------------------------------------------------------

    def port_in_use(self, port: int) -> bool:
        return port in self.busy_ports

    def trace_roots(self, *a: Any, **k: Any) -> dict:
        return {}

    def now(self) -> datetime:
        return self.clock

    def sleep(self, seconds: float) -> None:
        self.clock += timedelta(seconds=seconds)

    def as_host(self) -> Any:
        import dataclasses

        cls = mod("driver").Host
        given = {
            f.name: getattr(self, f.name)
            for f in dataclasses.fields(cls)
            if hasattr(self, f.name)
        }
        return cls(**given)


class Round:
    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch, **cfg: Any) -> None:
        d = mod("driver")
        monkeypatch.setattr(d, "verify_upstream", lambda *a, **k: None)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "ambient-deepseek")
        monkeypatch.setenv("LANGSMITH_API_KEY", "ambient-langsmith")
        self.upstream = tmp / "upstream"
        (self.upstream / "results").mkdir(parents=True)
        self.kubeconfig = tmp / "sregym.kubeconfig"
        self.fake = DriverHost(tmp, self.upstream)
        self.kubeconfig.write_text(yaml.safe_dump(self.fake.config))
        self.env_file = tmp / "secrets.env"
        self.env_file.write_text(
            f"DEEPSEEK_API_KEY={DEEPSEEK}\nLANGSMITH_API_KEY={LANGSMITH}\n"
            f"ZAI_API_KEY={JUDGE_KEY}\nJUDGE_API_KEY={JUDGE_KEY}\n"
        )
        self.env_file.chmod(0o600)
        self.evidence = tmp / "evidence"
        self.tmp_root = tmp / "tmp"
        baseline = tmp / "cache_dir" / "cluster_baseline_state.json"
        # Kafka wiring interface: prepare_round checks a hosts file; a temp one
        # with the advertised name keeps these passes off the real /etc/hosts.
        self.hosts_file = _hosts(tmp)
        fields: dict[str, Any] = {
            "round": "r1",
            "upstream": self.upstream,
            "kubeconfig": self.kubeconfig,
            "env_file": self.env_file,
            "tmp_root": self.tmp_root,
            "evidence_root": self.evidence,
            "baseline": baseline,
            "allow_dirty": True,
            "hosts_file": self.hosts_file,
        }
        fields.update(cfg)
        self.cfg = d.Config(**fields)
        self.host = self.fake.as_host()

    def prepare(self) -> list[Any]:
        return mod("driver").prepare_round(self.cfg, self.host)

    def trial(self, trial: int = 1) -> Any:
        return mod("driver").run_trial(self.cfg, self.host, PID, trial)

    def summaries(self) -> list[Path]:
        return sorted(self.evidence.rglob("summary.json"))

    def summary(self, trial: int = 1) -> dict[str, Any]:
        hits = [
            p
            for p in self.summaries()
            if p.parts[-3:] == (f"p{SEQ:02d}", f"trial-{trial}", "summary.json")
        ]
        assert len(hits) == 1, self.summaries()
        assert hits[0].parent.parent.parent.name == "sregym-lite-r1"
        return json.loads(hits[0].read_text())


@pytest.fixture
def rnd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Round:
    return Round(tmp_path, monkeypatch)


# --- group 4: main.py environment -----------------------------------------------------------------


def test_main_env_sets_the_cluster_registry_judge_and_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KUBECONFIG", "/home/eng/.kube/config")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ambient-deepseek")
    monkeypatch.setenv("LANGSMITH_API_KEY", "ambient-langsmith")
    d = mod("driver")
    kc = tmp_path / "sregym.kubeconfig"
    cfg = d.Config(
        round="r1",
        upstream=tmp_path / "up",
        kubeconfig=kc,
        tmp_root=tmp_path / "tmp",
        hosts_file=_hosts(tmp_path),
    )
    job = tmp_path / "job.json"
    env = d.main_env(cfg, JUDGE_KEY, job)
    assert env["KUBECONFIG"] == str(kc)
    assert env["KIND_CLUSTER_NAME"] == "sregym"
    assert Path(env["SREGYM_AGENT_REGISTRY"]) == cfg.round_dir / "agents.yaml"
    assert env["API_PORT"] == "28000"
    assert env["MCP_SERVER_PORT"] == "29954"
    assert env["K8S_PROXY_PORT"] == "26443"
    assert env["JUDGE_API_BASE"] == "https://api.z.ai/api/paas/v4"
    assert env["JUDGE_API_KEY"] == JUDGE_KEY
    assert env["SREGYM_EVAL_JOB"] == str(job)
    for k, v in env.items():
        assert k not in ("DEEPSEEK_API_KEY", "LANGSMITH_API_KEY"), k
        assert "ambient-deepseek" not in v and "ambient-langsmith" not in v
        assert DEEPSEEK not in v and LANGSMITH not in v


def test_main_env_refuses_an_empty_judge_key(tmp_path: Path) -> None:
    d = mod("driver")
    cfg = d.Config(
        round="r1",
        upstream=tmp_path / "up",
        kubeconfig=tmp_path / "kc",
        tmp_root=tmp_path / "t",
        hosts_file=_hosts(tmp_path),
    )
    with pytest.raises(_round_abort()) as info:
        d.main_env(cfg, "", tmp_path / "job.json")
    assert code_of(info.value) == "JUDGE_KEY_MISSING"


# --- results root lock ------------------------------------------------------------------------------


def test_results_lock_is_exclusive_and_released(tmp_path: Path) -> None:
    d = mod("driver")
    root = tmp_path / "results"
    root.mkdir()
    lock = d.ResultsLock(root)
    assert (root / ".opspilot-sregym.lock").exists()
    with pytest.raises(_round_abort()) as info:
        d.ResultsLock(root)
    assert code_of(info.value) == "RESULTS_ROOT_LOCKED"
    lock.release()
    assert not (root / ".opspilot-sregym.lock").exists()
    with d.ResultsLock(root):
        assert (root / ".opspilot-sregym.lock").exists()
    assert not (root / ".opspilot-sregym.lock").exists()


def test_run_dirs_lists_existing_run_directories(tmp_path: Path) -> None:
    d = mod("driver")
    root = tmp_path / "results"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    dirs = {Path(p).name for p in d.run_dirs(root)}
    assert {"a", "b"} <= dirs


# --- S-U2 product path probe ----------------------------------------------------------------------------


class ProbeHost:
    def __init__(self, get: int, post: int) -> None:
        self.status = {"GET": get, "POST": post}
        self.calls: list[tuple[str, str, str, str]] = []

    def http_probe(
        self, method: str, url: str, token: str, ca_file: str, body: bytes | None = None
    ) -> int:
        self.calls.append((method, url, token, ca_file))
        return self.status[method]


def _target() -> Any:
    return mod("identity").K8sTarget(
        api_url="https://127.0.0.1:26444", ca_file="/w/ca.crt", token="product-token"
    )


def test_probe_product_path_accepts_read_ok_and_write_forbidden() -> None:
    host = ProbeHost(200, 403)
    got = mod("filter_proxy").probe_product_path(host, _target())
    assert got == {"list_pods": 200, "dry_run_create_configmap": 403}
    methods = {m: (u, t, c) for m, u, t, c in host.calls}
    url, token, ca = methods["GET"]
    assert url.startswith("https://127.0.0.1:26444/api/v1/pods") and "limit=1" in url
    assert (token, ca) == ("product-token", "/w/ca.crt")
    url, token, ca = methods["POST"]
    assert url.startswith(
        "https://127.0.0.1:26444/api/v1/namespaces/default/configmaps"
    )
    assert "dryRun=All" in url
    assert (token, ca) == ("product-token", "/w/ca.crt")


@pytest.mark.parametrize(
    "get, post",
    [(200, 201), (200, 200), (403, 403), (401, 403), (0, 403), (200, 0), (200, 401)],
)
def test_probe_product_path_refuses_anything_else(get: int, post: int) -> None:
    with pytest.raises(mod("identity").IdentityError) as info:
        mod("filter_proxy").probe_product_path(ProbeHost(get, post), _target())
    assert code_of(info.value) == "PRODUCT_PATH_NOT_READONLY"


# --- recheck with the full problem ID ---------------------------------------------------------------------


def _recheck(**over: Any) -> None:
    args: dict[str, Any] = {
        "question": "Application: Social Network\nNamespace: social-network",
        "key": "sregym-r1-p03-t1",
        "project_name": "opspilot-sregym-r1",
        "record": {"app": {"app_name": "Social Network"}, "leak_check": "ok"},
    }
    args.update(over)
    mod("driver").recheck_leak(PID, **args)


def test_recheck_passes_clean_values() -> None:
    _recheck()
    _recheck(project_name=None)


@pytest.mark.parametrize(
    "over",
    [
        {"question": "Investigate the Network Policy Block now"},
        {"key": f"sregym-{PID}-t1"},
        {"project_name": f"opspilot-{PID}"},
        {"record": {"conversion_reason": PID.upper()}},
        {"record": {"app": {"app_name": "network-policy-block"}}},
    ],
)
def test_recheck_hit_aborts_with_answer_leak(over: dict[str, Any]) -> None:
    with pytest.raises(_round_abort()) as info:
        _recheck(**over)
    assert code_of(info.value) == "ANSWER_LEAK"
    detail = getattr(info.value, "detail", "")
    assert "PROBLEM_ID_IN:" in str(detail)
    assert PID not in str(detail) and SQUASHED_PID not in str(detail).replace("_", "")


# --- one pass, end to end -------------------------------------------------------------------------------


def test_pass_runs_main_once_with_the_contracted_command_and_env(rnd: Round) -> None:
    rnd.prepare()
    rnd.trial(1)
    f = rnd.fake
    assert len(f.main_calls) == 1
    call = f.main_calls[0]
    assert call["argv"] == mod("driver").main_command(PID)
    assert call["cwd"].resolve() == rnd.upstream.resolve()
    env = call["env"]
    assert env["KUBECONFIG"] == str(rnd.kubeconfig)
    assert env["KIND_CLUSTER_NAME"] == "sregym"
    assert env["JUDGE_API_BASE"] == "https://api.z.ai/api/paas/v4"
    assert env["JUDGE_API_KEY"] == JUDGE_KEY
    for k, v in env.items():
        assert k not in ("DEEPSEEK_API_KEY", "LANGSMITH_API_KEY")
        for secret in (DEEPSEEK, LANGSMITH, "ambient-deepseek", "ambient-langsmith"):
            assert secret not in v, k
    registry = yaml.safe_load(Path(env["SREGYM_AGENT_REGISTRY"]).read_text())
    assert [a["name"] for a in registry["agents"]] == ["opspilot"]
    assert call["job_mode"] == 0o600
    assert PID not in call["job_text"]
    assert (
        SQUASHED_PID not in call["job_text"].replace("_", "").replace("-", "").lower()
    )


def test_pass_order_token_and_can_i_before_main(rnd: Round) -> None:
    rnd.prepare()
    rnd.trial(1)
    ev = rnd.fake.events
    first_main = ev.index("run_main")
    assert ev.index("token") < first_main
    assert ev.index("can-i") < first_main
    assert ev.index("filter_proxy") < first_main
    assert ev.index("probe:GET") < first_main and ev.index("probe:POST") < first_main
    assert ev.index("gateway") < first_main
    assert "filter_proxy" in rnd.fake.stopped  # proxy stopped after main.py


def test_every_pass_gets_a_fresh_token_and_a_fresh_can_i(rnd: Round) -> None:
    rnd.prepare()
    rnd.trial(1)
    rnd.trial(2)
    ev = rnd.fake.events
    mains = [i for i, e in enumerate(ev) if e == "run_main"]
    assert len(mains) == 2
    between = ev[mains[0] + 1 : mains[1]]
    assert "token" in between and "can-i" in between
    assert rnd.fake.token_n == 2


def test_filter_proxy_runs_on_the_opspilot_proxy_port_with_the_readonly_kubeconfig(
    rnd: Round,
) -> None:
    rnd.prepare()
    rnd.trial(1)
    (upstream, readonly_kc, port, _work) = rnd.fake.proxy_calls[0]
    assert Path(upstream).resolve() == rnd.upstream.resolve()
    assert port == 26444
    text = Path(readonly_kc).read_text()
    assert "sa-token-1" in text and "engineer-admin-token" not in text
    # the product path probed is the proxy's, with the proxy-issued token
    assert all(t == "proxy-token" for _, _, t, _ in rnd.fake.probes)
    assert all(
        u.startswith("https://127.0.0.1:26444/") for _, u, _, _ in rnd.fake.probes
    )


def test_direct_readonly_skips_the_proxy_but_still_probes_and_discloses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = Round(tmp_path, monkeypatch, cluster_path="direct_readonly")
    r.prepare()
    r.trial(1)
    assert r.fake.proxy_calls == []
    assert {m for m, *_ in r.fake.probes} == {"GET", "POST"}
    assert all(t == "sa-token-1" for _, _, t, _ in r.fake.probes)
    s = r.summary(1)["sregym_eval"]
    assert s["cluster_path"] == "direct_readonly"
    assert "VISIBILITY_WIDER_THAN_OTHER_AGENTS" in s["disclosures"]
    assert s["boundary"]["product_path"] == {
        "list_pods": 200,
        "dry_run_create_configmap": 403,
    }


def test_summary_records_boundary_and_filter_proxy_path(rnd: Round) -> None:
    rnd.prepare()
    rnd.trial(1)
    summary = rnd.summary(1)
    assert summary["schema"] == "sregym-eval-summary-1"
    s = summary["sregym_eval"]
    assert s["problem_id"] == PID and s["trial"] == 1 and s["problem_seq"] == SEQ
    assert s["cluster_path"] == "filter_proxy_readonly"
    assert "VISIBILITY_SAME_FILTER_PROXY" in s["disclosures"]
    b = s["boundary"]
    assert b["rbac"] == []
    assert b["env_isolation"] == "ok"
    assert b["product_path"] == {"list_pods": 200, "dry_run_create_configmap": 403}
    assert len(b["can_i"]) == len(mod("boundary").CAN_I_CHECKS)
    assert "entry" in s
    assert "question_file" not in s["entry"]


def test_no_new_result_csv_is_env_broken_even_with_an_older_one(rnd: Round) -> None:
    old = (
        rnd.upstream
        / "results"
        / "run-old"
        / "opspilot"
        / PID
        / f"{PID}_opspilot_results.csv"
    )
    _write_csv(old)
    rnd.prepare()
    rnd.trial(1)
    s = rnd.summary(1)["sregym_eval"]
    assert s["result"] is None
    assert s["category"] == "env_broken"


def test_new_result_csv_is_read(rnd: Round) -> None:
    rnd.fake.write_result_csv = True
    rnd.prepare()
    rnd.trial(1)
    s = rnd.summary(1)["sregym_eval"]
    assert s["result"] is not None
    assert s["result"]["accuracy"] == 0.8


def test_results_root_lock_is_released_after_the_pass(rnd: Round) -> None:
    rnd.prepare()
    rnd.trial(1)
    assert not (rnd.upstream / "results" / ".opspilot-sregym.lock").exists()


# --- aborts: nothing runs, nothing is frozen ------------------------------------------------------------------


def _aborts_before_main(r: Round) -> BaseException:
    r.prepare()
    with pytest.raises(_round_abort()) as info:
        r.trial(1)
    assert r.fake.main_calls == []
    assert r.summaries() == []
    return info.value


@pytest.mark.parametrize(
    "verb, resource, answer",
    [
        ("create", "pods", "yes"),
        ("delete", "deployments.apps", "yes"),
        ("get", "secrets", "yes"),
        ("create", "pods/exec", "yes"),
        ("list", "pods", "no"),
        ("list", "pods", "error: Unauthorized"),
    ],
)
def test_can_i_mismatch_aborts_before_main(
    rnd: Round, verb: str, resource: str, answer: str
) -> None:
    rnd.fake.can_i_override[(verb, resource)] = answer
    _aborts_before_main(rnd)


def test_wrong_cluster_aborts_before_main(rnd: Round) -> None:
    rnd.prepare()
    rnd.fake.config = _kubeconfig(context="kind-holmes")
    rnd.kubeconfig.write_text(yaml.safe_dump(rnd.fake.config))
    with pytest.raises(_round_abort()) as info:
        rnd.trial(1)
    assert (
        "WRONG_CLUSTER" in f"{code_of(info.value)} {getattr(info.value, 'detail', '')}"
    )
    assert rnd.fake.main_calls == []


@pytest.mark.parametrize("get, post", [(200, 201), (403, 403), (0, 403)])
def test_product_path_not_readonly_aborts_before_main(
    rnd: Round, get: int, post: int
) -> None:
    rnd.fake.probe_status = {"GET": get, "POST": post}
    exc = _aborts_before_main(rnd)
    assert "PRODUCT_PATH_NOT_READONLY" in f"{code_of(exc)} {getattr(exc, 'detail', '')}"


def test_held_results_lock_aborts_before_main(rnd: Round) -> None:
    (rnd.upstream / "results" / ".opspilot-sregym.lock").write_text("other round")
    with pytest.raises(_round_abort()) as info:  # at prepare or at the pass
        rnd.prepare()
        rnd.trial(1)
    assert code_of(info.value) == "RESULTS_ROOT_LOCKED"
    assert rnd.fake.main_calls == []
    assert rnd.summaries() == []
    assert (
        rnd.upstream / "results" / ".opspilot-sregym.lock"
    ).read_text() == "other round"


def test_busy_port_aborts_before_main(rnd: Round) -> None:
    rnd.fake.busy_ports = {29580}
    _aborts_before_main(rnd)


def test_leak_in_entry_output_aborts_without_evidence(rnd: Round) -> None:
    rnd.fake.question = "Application: x\nThe fault is network-policy-block.\n"
    rnd.prepare()
    with pytest.raises(_round_abort()) as info:
        rnd.trial(1)
    assert code_of(info.value) == "ANSWER_LEAK"
    assert len(rnd.fake.main_calls) == 1
    assert rnd.summaries() == []


# --- resume ------------------------------------------------------------------------------------------------------


def test_existing_summary_is_skipped(rnd: Round) -> None:
    rnd.prepare()
    rnd.trial(1)
    before = rnd.summary(1)
    out = rnd.trial(1)
    assert out.status == "skipped"
    assert len(rnd.fake.main_calls) == 1
    assert rnd.summary(1) == before


def test_trial_outcome_shape(rnd: Round) -> None:
    rnd.prepare()
    out = rnd.trial(1)
    assert out.problem_id == PID and out.trial == 1
    assert out.status in ("completed", "missing")
    assert out.category == "env_broken"


def test_loki_gateway_is_started_on_29532_over_the_forward_and_recorded(
    rnd: Round,
) -> None:
    rnd.prepare()
    rnd.trial(1)
    assert rnd.fake.gateway == ("loki", 29532, "127.0.0.1:29531")
    plane = json.dumps(rnd.summary(1)["sregym_eval"]["data_plane"])
    assert "29532" in plane and "127.0.0.1:29531" in plane
