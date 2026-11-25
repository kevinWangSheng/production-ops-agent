"""Unit tests of the ITBench live harness (#564) on synthetic data only.

No cluster, no ITBench checkout, no model: the host is a fake. The synthetic
answer files are placeholders written by the test, never upstream answers.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.itbench_lite.services import Started
from scripts.itbench_live import (
    alerts,
    driver,
    judge_trial,
    live_match,
    upstream,
    vocabulary,
)
from scripts.sregym_eval.boundary import CAN_I_CHECKS

# -- upstream ------------------------------------------------------------------------


def _library(root: Path, ids: Sequence[str], *, answer: bool = True) -> Path:
    for sid in ids:
        d = root / "library" / "scenarios" / sid
        d.mkdir(parents=True)
        (d / "scenario.yaml").write_text("x: 1\n")
        if answer:
            (d / "ground_truth.yaml").write_text("entities: []\n")
    return root


def test_scenario_ids_by_name_only_without_opening_files(tmp_path: Path) -> None:
    checkout = _library(tmp_path / "itb", ["102", "20", "37", "38", "23"])
    (checkout / "library" / "scenarios" / "notes").mkdir()
    _library(tmp_path / "itb2", ["5"], answer=False)
    # Unreadable files: listing must not open them.
    for path in (checkout / "library" / "scenarios").rglob("*.yaml"):
        path.chmod(0)
    try:
        assert upstream.scenario_ids(checkout) == ["20", "23", "102"]
    finally:
        for path in (checkout / "library" / "scenarios").rglob("*.yaml"):
            path.chmod(0o600)
    upstream.check_scenario(checkout, "20")
    for bad, code in (
        ("37", "SCENARIO_FINOPS"),
        ("21", "SCENARIO_UNKNOWN"),
        ("x", "SCENARIO_ID_INVALID"),
    ):
        with pytest.raises(upstream.UpstreamError) as info:
            upstream.check_scenario(checkout, bad)
        assert info.value.code == code


def test_cli_argv_and_env(tmp_path: Path) -> None:
    kube = tmp_path / "kc"
    assert upstream.cli_argv("deploy", kube, "20")[4:] == [
        "deploy",
        "environment",
        "--scenario",
        "20",
        "--kubeconfig",
        str(kube),
    ]
    assert upstream.cli_argv("inject", kube, "20") == [
        "uv",
        "run",
        "--no-dev",
        "itbench",
        "inject",
        "faults",
        "--scenario",
        "20",
        "--kubeconfig",
        str(kube),
    ]
    assert upstream.cli_argv("undeploy", kube)[-1] == "--yes"
    with pytest.raises(ValueError):
        upstream.cli_argv("inject", kube)
    env = upstream.cli_env(
        {
            "PATH": "/bin",
            "KUBECONFIG": "/x",
            "DEEPSEEK_API_KEY": "k",
            "ZAI_API_KEY": "z",
        }
    )
    assert (
        "KUBECONFIG" not in env
        and "DEEPSEEK_API_KEY" not in env
        and "ZAI_API_KEY" not in env
    )
    assert env["PATH"] == "/bin"


def test_run_cli_output_goes_to_log_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    def fake_run(
        argv: Sequence[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        seen.update(argv=list(argv), cwd=kwargs["cwd"])
        kwargs["stdout"].write(b"ANSWER-BEARING ANSIBLE TEXT\n")
        return subprocess.CompletedProcess(list(argv), 2, "", "")

    result = upstream.run_cli(
        tmp_path,
        "inject",
        tmp_path / "kc",
        tmp_path / "logs" / "inject.log",
        scenario="20",
        run=fake_run,
    )
    assert result.returncode == 2 and seen["cwd"] == tmp_path
    assert "ANSWER-BEARING" in (tmp_path / "logs" / "inject.log").read_text()
    assert "ANSWER-BEARING" not in capsys.readouterr().out
    with pytest.raises(upstream.UpstreamError) as info:
        upstream.require_ok(result)
    assert info.value.code == "ITBENCH_INJECT_FAILED"


def _git_head(head: str) -> Any:
    def run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, head + "\n", "")

    return run


def test_check_pinned() -> None:
    ok = _git_head(upstream.ITBENCH_COMMIT)
    assert upstream.check_pinned(Path("/x"), ok) == upstream.ITBENCH_COMMIT
    with pytest.raises(upstream.UpstreamError):
        upstream.check_pinned(Path("/x"), _git_head("deadbeef"))


# -- alerts --------------------------------------------------------------------------


def _alert(
    name: str, active: str, state: str = "firing", **labels: str
) -> dict[str, Any]:
    return {
        "labels": {"alertname": name, **labels},
        "annotations": {"summary": "untrusted text"},
        "state": state,
        "activeAt": active,
        "value": "1",
    }


def _body(*items: dict[str, Any]) -> dict[str, Any]:
    return {"status": "success", "data": {"alerts": list(items)}}


def test_entry_alert_skips_baseline_and_prefault() -> None:
    noise = _alert(
        "KubePodNotReady", "2026-11-24T10:00:00Z", namespace="otel-demo", pod="a-1"
    )
    pending = _alert(
        "Other", "2026-11-24T10:00:30Z", state="pending", namespace="otel-demo"
    )
    excluded = alerts.prefault_active(alerts.parse_alerts(_body(noise, pending)))
    assert len(excluded) == 2
    watchdog = _alert("Watchdog", "2026-11-24T09:00:00Z")
    late = _alert(
        "KubeDeploymentRolloutStuck",
        "2026-11-24T10:06:00Z",
        namespace="otel-demo",
        deployment="cart",
    )
    early = _alert(
        "RequestErrorRate",
        "2026-11-24T10:05:00Z",
        namespace="otel-demo",
        service_name="cart",
    )
    still_pending = _alert(
        "X", "2026-11-24T10:01:00Z", state="pending", namespace="otel-demo"
    )
    now = alerts.parse_alerts(
        _body(noise, pending, watchdog, late, early, still_pending)
    )
    chosen = alerts.entry_alert(now, excluded)
    assert chosen is not None and chosen["labels"]["alertname"] == "RequestErrorRate"
    assert (
        alerts.entry_alert(alerts.parse_alerts(_body(noise, watchdog)), excluded)
        is None
    )
    with pytest.raises(alerts.AlertsUnreadable):
        alerts.parse_alerts({"status": "error"})


def test_entry_alert_not_before_inject_and_reactivation() -> None:
    """Alerts active before the inject command started are never the entry
    (the snapshot-to-inject gap included); a pre-fault label set that
    re-activates after injection, with a new activeAt, is eligible."""
    inject = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
    old = _alert("Old", "2026-11-24T09:50:00Z", namespace="otel-demo")
    prefault = alerts.prefault_active([old])
    gap = _alert("Gap", "2026-11-24T09:59:59Z", namespace="otel-demo")
    assert alerts.entry_alert([old, gap], prefault, inject) is None
    # Without the floor the gap alert would have been chosen.
    assert alerts.entry_alert([old, gap], prefault)["labels"]["alertname"] == "Gap"
    again = dict(old, activeAt="2026-11-24T10:02:00Z")
    later = _alert("Later", "2026-11-24T10:03:00Z", namespace="otel-demo")
    chosen = alerts.entry_alert([again, gap, later], prefault, inject)
    assert chosen is not None and chosen["labels"]["alertname"] == "Old"
    at_mark = _alert("AtMark", "2026-11-24T10:00:00Z", namespace="otel-demo")
    assert alerts.entry_alert([at_mark], prefault, inject) is not None


def test_webhook_keeps_environment_active_at() -> None:
    entry = _alert(
        "RequestErrorRate",
        "2026-11-24T10:05:00.123Z",
        namespace="otel-demo",
        service_name="cart",
    )
    body = alerts.webhook(entry, {"service_name": "cart"})
    assert body["version"] == "4" and body["status"] == "firing"
    (item,) = body["alerts"]
    assert item["startsAt"].startswith("2026-11-24T10:05:00")
    assert item["labels"]["service_name"] == "cart"
    with pytest.raises(ValueError):
        alerts.webhook(entry, {"service_name": "other"})
    kept = alerts.summary_of(entry)
    assert "annotations" not in kept and kept["alertname"] == "RequestErrorRate"


# -- vocabulary ----------------------------------------------------------------------


def _items(*objs: tuple[str, str | None]) -> dict[str, Any]:
    return {
        "items": [
            {"metadata": {"name": n, **({"namespace": ns} if ns else {})}}
            for n, ns in objs
        ]
    }


def test_vocabulary_rules() -> None:
    before = {
        "pods": _items(("cart-7d9f-abc", "otel-demo"), ("coredns-1", "kube-system")),
        "deployments": _items(("cart", "otel-demo")),
        "services": _items(("cart", "otel-demo"), ("default", "otel-demo")),
        "secrets": _items(("cart-password", "otel-demo")),
        "namespaces": _items(("otel-demo", None), ("kube-system", None)),
        "nodes": _items(("kind-environment-worker", None)),
    }
    after = {"pods": _items(("cart-7d9f-xyz", "otel-demo"))}
    names = vocabulary.build([before, after], ["frontend", "otel-demo"])
    assert names == sorted(names, key=str.casefold)
    assert {
        "cart",
        "cart-7d9f-abc",
        "cart-7d9f-xyz",
        "frontend",
        "kind-environment-worker",
    } <= set(names)
    for absent in ("cart-password", "coredns-1", "otel-demo", "default", "kube-system"):
        assert absent not in names
    kinds = vocabulary.build_kinds([before, after])
    assert kinds["cart"] == [["Deployment", "otel-demo"], ["Service", "otel-demo"]]
    assert kinds["kind-environment-worker"] == [["Node", None]]
    assert vocabulary.jaeger_service_names({"data": ["a", 3]}) == ["a"]
    assert (
        "secrets" not in vocabulary.LIST_PLURALS
        and "namespaces" in vocabulary.LIST_PLURALS
    )


# -- deterministic check -------------------------------------------------------------


def _output(*names: str, extra: Sequence[str] = ()) -> dict[str, Any]:
    entities = [{"name": n, "contributing_factor": True} for n in names]
    entities += [{"name": n, "contributing_factor": False} for n in extra]
    return {"entities": entities, "propagations": [], "alerts_explained": []}


def test_live_match_flat_and_wrapped_answers() -> None:
    flat = {
        "entities": [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "name": "svc-a",
                "namespace": "ns",
            }
        ]
    }
    wrapped = {
        "entities": [
            {"kubernetes": [{"kind": "Deployment", "name": "svc-a", "namespace": "ns"}]}
        ]
    }
    for gt in (flat, wrapped):
        hit = live_match.score(_output("ns/Deployment/svc-a"), gt)
        assert hit["any_hit"] is True and hit["f1"] == 1.0
        wrong_kind = live_match.score(_output("ns/Pod/svc-a"), gt)
        assert wrong_kind["any_hit"] is False and wrong_kind["f1"] == 0.0
        prefix = live_match.score(_output("ns/Deployment/svc-ab"), gt)
        assert prefix["any_hit"] is False
    assert (
        live_match.score(_output(extra=["ns/Deployment/svc-a"]), flat)["any_hit"]
        is False
    )


def test_judge_trial_deterministic_only(tmp_path: Path) -> None:
    gt_root = tmp_path / "gt"
    (gt_root / "20").mkdir(parents=True)
    (gt_root / "20" / "ground_truth.yaml").write_text(
        "# comment\nentities:\n  - {kind: Deployment, name: svc-a, namespace: ns}\nalerts: []\nsolutions: []\n"
    )
    trial = tmp_path / "outputs" / "20" / "1"
    (trial / "outputs").mkdir(parents=True)
    (trial / "outputs" / "agent_output.json").write_text(
        json.dumps(_output("ns/Deployment/svc-a"))
    )
    (trial / "conversion_meta.json").write_text(json.dumps({"report_present": True}))
    out = tmp_path / "scores.json"
    code = judge_trial.main(
        [
            "--ground-truth",
            str(gt_root),
            "--scenario",
            "20",
            "--outputs",
            str(tmp_path / "outputs"),
            "--trial",
            "1",
            "--work-dir",
            str(tmp_path / "w"),
            "--out",
            str(out),
        ]
    )
    data = json.loads(out.read_text())
    assert code == 0 and data["official"] is None and data["judged"] is False
    assert data["det"]["det_any_hit"] == 1.0 and data["det"]["det_f1"] == 1.0
    assert "svc-a" not in out.read_text()


# -- driver --------------------------------------------------------------------------


CA = base64.b64encode(
    b"-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n"
).decode()


class FakeHost(driver.Host):
    """The machine, synthetically: kubectl, the CLI, forwards, the product."""

    def __init__(
        self, alert_bodies: list[dict[str, Any]], *, judge_rc: int = 0
    ) -> None:
        self.calls: list[list[str]] = []
        self.alert_reads: list[str] = []
        self.cli: list[list[str]] = []
        self.alert_bodies = alert_bodies
        self.stopped: list[str] = []
        self.delivered: list[Mapping[str, Any]] = []
        self.envs: dict[str, Mapping[str, str]] = {}
        self.clock = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
        self.judge_rc = judge_rc
        self.prometheus_cr: dict[str, Any] = {"spec": {"replicas": 1}}
        self.span_body: Any = {"status": "success", "data": {"result": [
            {"metric": {"namespace": "otel-demo", "service_name": "cart"}, "value": [1.0, "4"]}]}}  # fmt: skip
        self.sleeps: list[tuple[float, int]] = []
        self.jobs: list[Mapping[str, Any]] = []
        self.nodes: dict[str, Any] = {"items": [
            {"metadata": {"name": "kind-environment-worker"}, "spec": {}},
            {"metadata": {"name": "kind-environment-worker2"}, "spec": {"taints": [
                {"key": "itbench.io/sandbox", "value": "true", "effect": "NoSchedule"}]}},
        ]}  # fmt: skip
        super().__init__(
            run=self._run, kubectl_fn=self._kubectl, port_forward=self._forward,
            get_json=self._get, start_pg=lambda d, p: SimpleNamespace(
                web_dsn="postgresql://web@127.0.0.1:29610/x",
                runtime_dsn="postgresql://rt@127.0.0.1:29610/x",
                owner_dsn="postgresql://own@127.0.0.1:29610/x"),
            stop_pg=lambda pg: self.stopped.append("pg"),
            start_web=self._web, start_worker=self._worker, deliver=self._deliver,
            run_state=lambda dsn, rid: "succeeded",
            collect=lambda dsn, rid: ({"run_id": rid, "report": None, "outcome": None}, {"rows": []}),
            trace_roots=lambda project, rid, env: {"read_back": "skipped"},
            judge=self._judge, port_in_use=lambda p: False, memory_reading=lambda: None,
            now=self._now, monotonic=lambda: 0.0, sleep=self._sleep,
        )  # fmt: skip

    def _now(self) -> datetime:
        return self.clock

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append((seconds, len(self.alert_reads)))
        self.clock += timedelta(seconds=seconds)

    def _run(
        self, argv: Sequence[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.cli.append(list(argv))
        if "stdout" in kwargs and hasattr(kwargs["stdout"], "write"):
            kwargs["stdout"].write(b"upstream output\n")
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    def _kubectl(
        self, argv: Sequence[str], env: Mapping[str, str], timeout: float
    ) -> Any:
        argv = list(argv)
        self.calls.append(argv)
        out = ""
        if argv[1:3] == ["config", "view"]:
            out = json.dumps(
                {
                    "clusters": [
                        {
                            "cluster": {
                                "server": "https://127.0.0.1:43210",
                                "certificate-authority-data": CA,
                            }
                        }
                    ]
                }
            )
        elif argv[1:3] == ["config", "current-context"]:
            out = "kind-kind-environment\n"
        elif argv[1:3] == ["auth", "can-i"]:
            verb, resource = argv[3], argv[4]
            sub = next(
                (a.split("=", 1)[1] for a in argv if a.startswith("--subresource=")), ""
            )
            full = f"{resource}/{sub}" if sub else resource
            out = (
                next(
                    c.expect
                    for c in CAN_I_CHECKS
                    if c.verb == verb and c.resource == full
                )
                + "\n"
            )
        elif "create" in argv and "token" in argv:
            out = "synthetic-readonly-token\n"
        elif argv[1:] == ["get", "nodes", "-o", "json"]:
            out = json.dumps(self.nodes)
        elif argv[1:4] == ["get", "namespace", "kube-system"]:
            out = "uid-kube-system"
        elif argv[3:5] == ["get", "prometheus"]:
            out = json.dumps(self.prometheus_cr)
        elif argv[1] == "get" and argv[3:] == ["-A", "-o", "json"]:
            plural = argv[2]
            data = {
                "deployments": _items(("cart", "otel-demo")),
                "pods": _items(("cart-1", "otel-demo")),
                "namespaces": _items(("otel-demo", None)),
            }
            out = json.dumps(data.get(plural, {"items": []}))
        return subprocess.CompletedProcess(argv, 0, out, "")

    def _forward(
        self,
        ns: str,
        resource: str,
        local: int,
        remote: int,
        env: Mapping[str, str],
        log: Path,
    ) -> Started:
        started = Started(f"pf:{local}", lambda: self.stopped.append(f"pf:{local}"))
        started.info = {"pid": 40000 + local % 1000}
        return started

    def _get(self, url: str) -> tuple[int, Any]:
        if "/api/v1/query?" in url and "traces_span_metrics_calls_total" in url:
            return 200, self.span_body
        if url.endswith("/api/v1/alerts"):
            self.alert_reads.append(url)
            body = (
                self.alert_bodies[0]
                if len(self.alert_bodies) == 1
                else self.alert_bodies.pop(0)
            )
            return 200, body
        if url.endswith("/api/services"):
            return 200, {"data": ["cart", "frontend"]}
        return 404, None

    def _web(self, env: Mapping[str, str], port: int, log: Path) -> Started:
        self.envs["web"] = env
        return Started("web", lambda: self.stopped.append("web"))

    def _worker(
        self, env: Mapping[str, str], log: Path, *, settle_seconds: float
    ) -> Started:
        self.envs["worker"] = env
        return Started(
            "worker", lambda: self.stopped.append("worker"), alive=lambda: True
        )

    def _deliver(
        self, url: str, token: str, body: Mapping[str, Any]
    ) -> tuple[int, Any]:
        self.delivered.append(body)
        return 200, {
            "results": [{"outcome": "created", "run_id": "run-1", "reason": None}]
        }

    def _judge(self, job: Mapping[str, Any]) -> int:
        self.jobs.append(job)
        Path(job["out"]).write_text(json.dumps({
            "report_present": False, "det": {"det_any_hit": 0.0, "det_f1": 0.0},
            "official": {"root_cause_entity_f1": 0.0}, "judged": True, "labels": {},
        }))  # fmt: skip
        return self.judge_rc


def _cfg(tmp_path: Path) -> driver.Config:
    itbench = _library(tmp_path / "itbench", ["20", "23"])
    kube = tmp_path / "admin" / "kubeconfig"
    kube.parent.mkdir()
    kube.write_text("users:\n- user:\n    token: admin-secret-token-value\n")
    return driver.Config(
        round="unit", itbench=itbench, kubeconfig=kube, trace="off",
        tmp_root=tmp_path / "repo" / "tmp", evidence_root=tmp_path / "repo" / "evidence",
        alert_wait=timedelta(minutes=2), poll_seconds=15.0, worker_settle_seconds=0.0,
        settle=timedelta(seconds=60),
        secrets_env={"DEEPSEEK_API_KEY": "sk-synthetic"},
    )  # fmt: skip


@pytest.fixture(autouse=True)
def _signal_state() -> Any:
    yield
    driver.SIGNALS.deferring, driver.SIGNALS.pending = False, None
    driver.SIGNALS.fired = False


@pytest.fixture(autouse=True)
def _ledger_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import lab_evidence

    monkeypatch.setenv(lab_evidence.LEDGER_DIR_ENV, str(tmp_path / "ledgers"))


def test_trial_end_to_end(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    noise = _alert(
        "KubePodNotReady", "2026-11-24T09:59:00Z", namespace="otel-demo", pod="x-1"
    )
    entry = _alert(
        "RequestErrorRate",
        "2026-11-24T10:03:00Z",
        namespace="otel-demo",
        service_name="cart",
    )
    host = FakeHost([_body(noise), _body(noise), _body(noise, entry)])
    block = driver.Trial(cfg, host, "20", 1).run()

    assert block["status"] == "completed", block
    commands = [a[4:6] for a in host.cli]
    assert commands == [
        ["configure", "environment"],
        ["deploy", "environment"],
        ["inject", "faults"],
        ["remove", "faults"],
        ["undeploy", "environment"],
    ]
    assert block["cleanup"]["errors"] == []
    assert block["sandbox_nodes"] == ["kind-environment-worker2"]
    order = block["cleanup"]["order"]
    assert order[0] == "worker" and order[-1] == "undeploy"
    assert order.index("pg") < order.index("remove-faults") < order.index("undeploy")
    (delivered,) = host.delivered
    assert delivered["alerts"][0]["labels"]["alertname"] == "RequestErrorRate"
    identities = json.loads(
        (cfg.trial_dir("20", 1) / "target_identities.json").read_text()
    )
    (entry_cfg,) = identities.values()
    assert (
        entry_cfg["cluster_uid"] == "uid-kube-system"
        and entry_cfg["workload"] == "cart"
    )
    assert entry_cfg["match"]["labels"] == {"service_name": "cart"}
    # Isolation: the admin kubeconfig, the ITBench path and the judge never reach the product.
    for role in ("web", "worker"):
        values = " ".join(host.envs[role].values())
        assert (
            "admin-secret-token-value" not in values and str(cfg.itbench) not in values
        )
    assert "DEEPSEEK_API_KEY" not in host.envs["web"]
    assert host.envs["worker"]["OPSPILOT_K8S_TOKEN"] == "synthetic-readonly-token"
    assert host.envs["worker"]["OPSPILOT_K8S_NAMESPACE"] == "otel-demo"
    summary = json.loads((cfg.evidence_trial("20", 1) / "summary.json").read_text())
    assert summary["status"] == "completed" and summary["run_id"] == "run-1"
    live = summary["itbench_live"]
    assert live["fault_start_proxy"] == driver.FAULT_START_PROXY
    assert set(live["diagnosis"]) == {
        "from_fault_start_proxy_seconds",
        "from_delivery_seconds",
    }
    assert "annotations" not in json.dumps(live["entry_alert"])
    assert (
        cfg.trial_dir("20", 1)
        / "outputs"
        / "20"
        / "1"
        / "outputs"
        / "agent_output.json"
    ).is_file()
    # No kubectl call ever reads a Secret.
    assert not any("secrets" in a[2:3] for a in host.calls if a[1:2] == ["get"])

    scores = driver.judge_one(cfg, host, "20", 1)
    (job,) = host.jobs
    assert (
        job["ground_truth"] == cfg.itbench / "library" / "scenarios"
        and job["scenario"] == "20"
    )
    assert scores["status"] == "ok"
    table = driver.aggregate(cfg.evidence_dir)
    assert table["trials"] == 1 and table["scored"] == 1
    assert table["rows"][0]["root_cause_entity_f1"] == 0.0


def test_trial_without_entry_alert_is_recorded_and_cleaned(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    noise = _alert(
        "KubePodNotReady", "2026-11-24T09:59:00Z", namespace="otel-demo", pod="x-1"
    )
    host = FakeHost([_body(noise)])
    block = driver.Trial(cfg, host, "23", 1).run()
    assert block["status"] == "stopped" and block["reason"] == "NO_ENTRY_ALERT"
    assert host.delivered == [] and "web" not in host.envs
    assert [a[4] for a in host.cli][-2:] == ["remove", "undeploy"]
    summary = json.loads((cfg.evidence_trial("23", 1) / "summary.json").read_text())
    assert summary["status"] == "stopped" and summary["reason"] == "NO_ENTRY_ALERT"
    assert driver.aggregate(cfg.evidence_dir)["scored"] == 0


def test_can_i_mismatch_stops_before_fault(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    host = FakeHost([_body()])
    original = host.kubectl_fn

    def wide(argv: Sequence[str], env: Mapping[str, str], timeout: float) -> Any:
        done = original(argv, env, timeout)
        if list(argv[1:3]) == ["auth", "can-i"] and argv[4] == "secrets":
            return subprocess.CompletedProcess(list(argv), 0, "yes\n", "")
        return done

    host.kubectl_fn = wide
    block = driver.Trial(cfg, host, "20", 1).run()
    assert block["status"] == "stopped" and block["reason"] == "CAN_I_MISMATCH"
    assert [a[4] for a in host.cli] == ["configure", "deploy", "undeploy"]


def _failing_cli(host: FakeHost, command: str, times: int) -> None:
    """The first ``times`` calls of one ITBench CLI command exit 1."""
    real, left = host.run, [times]

    def failing(argv: Sequence[str], **kwargs: Any) -> Any:
        done = real(argv, **kwargs)
        if argv[4] == command and left[0] > 0:
            left[0] -= 1
            return subprocess.CompletedProcess(list(argv), 1, "", "")
        return done

    host.run = failing


def test_failed_undeploy_aborts_round(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    host = FakeHost([_body()])
    _failing_cli(host, "undeploy", 2)
    with pytest.raises(driver.RoundAbort) as info:
        driver.Trial(cfg, host, "20", 1).run()
    assert info.value.code == "CLEANUP_FAILED"
    assert [a[4] for a in host.cli][-2:] == ["undeploy", "undeploy"]


def test_undeploy_retried_once_reaches_cleanup_evidence(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    entry = _alert("RequestErrorRate", "2026-11-24T10:03:00Z", service_name="cart")
    host = FakeHost([_body(), _body(entry)])
    _failing_cli(host, "undeploy", 1)
    block = driver.Trial(cfg, host, "20", 1).run()
    assert block["status"] == "completed"
    assert [a[4] for a in host.cli][-2:] == ["undeploy", "undeploy"]
    # The completed summary is frozen before the cleanup; the codes land here.
    cleanup = json.loads((cfg.evidence_trial("20", 1) / "cleanup.json").read_text())
    assert cleanup == {
        "errors": [], "disclosures": ["UNDEPLOY_RETRIED:ITBENCH_UNDEPLOY_FAILED"],
    }  # fmt: skip


def test_failed_remove_aborts_round_after_undeploy(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    host = FakeHost([_body()])
    _failing_cli(host, "remove", 1)
    with pytest.raises(driver.RoundAbort) as info:
        driver.Trial(cfg, host, "20", 1).run()
    assert info.value.code == "CLEANUP_FAILED" and "remove-faults" in str(info.value)
    assert [a[4] for a in host.cli][-2:] == ["remove", "undeploy"]


@pytest.mark.parametrize("command", ["remove", "undeploy"])
def test_signal_during_cleanup_is_deferred(tmp_path: Path, command: str) -> None:
    import signal

    cfg = _cfg(tmp_path)
    host = FakeHost([_body()])
    real, finished = host.run, []

    def signalled(argv: Sequence[str], **kwargs: Any) -> Any:
        if argv[4] == command:
            os.kill(os.getpid(), signal.SIGTERM)
            os.kill(os.getpid(), signal.SIGHUP)
            finished.append(command)
        return real(argv, **kwargs)

    host.run = signalled
    previous = driver.install_signal_handlers()
    try:
        with pytest.raises(driver.DriverKilled) as info:
            driver.Trial(cfg, host, "20", 1).run()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    assert info.value.code == "SIGNAL_SIGTERM" and finished == [command]
    assert [a[4] for a in host.cli][-2:] == ["remove", "undeploy"]
    trial = json.loads((cfg.trial_dir("20", 1) / "trial.json").read_text())
    assert trial["cleanup"]["errors"] == [] and trial["reason"] == "NO_ENTRY_ALERT"
    assert trial["signal_during_cleanup"] == "SIGNAL_SIGTERM"


def _namespaces(host: FakeHost, present: Mapping[str, int]) -> list[str]:
    """``present[ns]``: how many namespace reads still find ``ns``."""
    left, seen, original = dict(present), [], host.kubectl_fn

    def kubectl(argv: Sequence[str], env: Mapping[str, str], timeout: float) -> Any:
        if list(argv[1:3]) == ["get", "namespace"] and "--ignore-not-found" in argv:
            seen.append(argv[3])
            if left.get(argv[3], 0) != 0:
                left[argv[3]] -= 1
                return subprocess.CompletedProcess(
                    list(argv), 0, f"namespace/{argv[3]}\n", ""
                )
            return subprocess.CompletedProcess(list(argv), 0, "", "")
        return original(argv, env, timeout)

    host.kubectl_fn = kubectl
    return seen


def test_deploy_retried_once(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    host = FakeHost([_body()])
    _failing_cli(host, "deploy", 1)
    block = driver.Trial(cfg, host, "20", 1).run()
    assert (
        block["reason"] == "NO_ENTRY_ALERT" and "DEPLOY_RETRIED" in block["disclosures"]
    )
    assert [a[4] for a in host.cli][:3] == ["configure", "deploy", "deploy"]

    host = FakeHost([_body()])
    _failing_cli(host, "deploy", 2)
    block = driver.Trial(cfg, host, "23", 1).run()
    assert (
        block["status"] == "infra_failed" and block["reason"] == "ITBENCH_DEPLOY_FAILED"
    )
    assert [a[4] for a in host.cli] == ["configure", "deploy", "deploy", "undeploy"]


def test_dirty_cluster_aborts_round_before_configure(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    host = FakeHost([_body()])
    seen = _namespaces(host, {"itbench-observability": -1})
    with pytest.raises(driver.RoundAbort) as info:
        driver.Trial(cfg, host, "20", 1).run()
    assert info.value.code == "ENV_NOT_CLEAN"
    assert set(seen) == {"otel-demo", "itbench-observability"}
    assert host.cli == []
    assert not (cfg.evidence_trial("20", 1) / "summary.json").exists()


def _otel_demo_reads(host: FakeHost, present: Callable[[int], bool]) -> None:
    """The n-th read (1-based) of namespace otel-demo finds it iff ``present(n)``."""
    reads, real = [0], host.kubectl_fn

    def kubectl(argv: Sequence[str], env: Mapping[str, str], timeout: float) -> Any:
        if (
            list(argv[1:4]) == ["get", "namespace", "otel-demo"]
            and "--ignore-not-found" in argv
        ):
            reads[0] += 1
            out = "namespace/otel-demo\n" if present(reads[0]) else ""
            return subprocess.CompletedProcess(list(argv), 0, out, "")
        return real(argv, env, timeout)

    host.kubectl_fn = kubectl


def test_namespace_wait_after_undeploy(tmp_path: Path) -> None:
    # Read 1 is before configure; reads 2-3 (after undeploy) still find it.
    cfg, host = _cfg(tmp_path / "a"), FakeHost([_body()])
    _otel_demo_reads(host, lambda n: n in (2, 3))
    block = driver.Trial(cfg, host, "20", 1).run()
    assert block["status"] == "stopped" and block["cleanup"]["errors"] == []

    cfg, host = _cfg(tmp_path / "b"), FakeHost([_body()])
    _otel_demo_reads(host, lambda n: n > 1)
    with pytest.raises(driver.RoundAbort) as info:
        driver.Trial(cfg, host, "20", 1).run()
    assert info.value.code == "CLEANUP_FAILED" and "undeploy" in str(info.value)
    trial = json.loads((cfg.trial_dir("20", 1) / "trial.json").read_text())
    cleaned = datetime.fromisoformat(trial["times"]["cleaned_up"])
    injected = datetime.fromisoformat(trial["times"]["inject_done"])
    assert cleaned - injected >= cfg.alert_wait + cfg.namespace_wait


def test_preflight_refuses_other_kind_clusters(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    host = FakeHost([_body()])
    host.run = lambda *a, **k: subprocess.CompletedProcess(
        [], 0, upstream.ITBENCH_COMMIT, ""
    )
    original = host.kubectl_fn

    def kind(argv: Sequence[str], env: Mapping[str, str], timeout: float) -> Any:
        if list(argv[:3]) == ["kind", "get", "clusters"]:
            return subprocess.CompletedProcess(
                list(argv), 0, "kind-environment\nopspilot-m1\n", ""
            )
        return original(argv, env, timeout)

    host.kubectl_fn = kind
    cfg.judge = False
    with pytest.raises(driver.RoundAbort) as info:
        driver.preflight(cfg, host)
    assert info.value.code == "OTHER_KIND_CLUSTERS"
    cfg.allow_other_clusters = True
    out = driver.preflight(cfg, host)
    assert out["scenarios"] == ["20", "23"] and out["other_kind_clusters"] == [
        "opspilot-m1"
    ]


def test_resolvers() -> None:
    assert driver.resolve_trials("3") == (1, 2, 3)
    assert driver.resolve_trials("2-3") == (2, 3)
    assert driver.resolve_scenarios("S20, 23") == ("20", "23")
    with pytest.raises(Exception):
        driver.resolve_scenarios("20,x")


def test_judge_env_excludes_product_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.itbench_lite.driver import judge_env

    env = judge_env(
        {
            "PATH": "/bin",
            "ZAI_API_KEY": "z",
            "DEEPSEEK_API_KEY": "d",
            "KUBECONFIG": "/k",
        }
    )
    assert (
        "DEEPSEEK_API_KEY" not in env
        and "KUBECONFIG" not in env
        and env["ZAI_API_KEY"] == "z"
    )
    assert os.environ is not env


class V3OnlyJaeger(FakeHost):
    """Smoke 2026-11-24: Jaeger answers only the v3 query API."""

    def _get(self, url: str) -> tuple[int, Any]:
        if url.endswith("/api/v3/services"):
            return 200, {"services": ["cart", "frontend-proxy"]}
        if url.endswith("/api/services"):
            return 404, None
        return super()._get(url)


def test_jaeger_v3_only_is_disclosed_and_feeds_vocabulary(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    entry = _alert(
        "RequestErrorRate",
        "2026-11-24T10:03:00Z",
        namespace="otel-demo",
        service_name="cart",
    )
    host = V3OnlyJaeger([_body(), _body(entry)])
    block = driver.Trial(cfg, host, "20", 1).run()
    assert block["status"] == "completed"
    assert driver.JAEGER_V3_ONLY in block["disclosures"]
    assert "JAEGER_QUERY_UNANSWERED" not in block["disclosures"]
    vocab = (cfg.trial_dir("20", 1) / "vocabulary.txt").read_text().split()
    assert "frontend-proxy" in vocab


def test_missing_sandbox_taint_aborts_round_before_deploy(tmp_path: Path) -> None:
    """Smoke 2026-11-25: without ``itbench configure environment`` taking
    effect, a scenario's inject stops at upstream's worker-node check. It is
    cluster-level: the round stops and the trial is not recorded as done."""
    cfg = _cfg(tmp_path)
    host = FakeHost([_body()])
    host.nodes = {"items": [{"metadata": {"name": "w"}, "spec": {"taints": [
        {"key": "itbench.io/sandbox", "value": "true", "effect": "NoExecute"}]}}]}  # fmt: skip
    with pytest.raises(driver.RoundAbort) as info:
        driver.Trial(cfg, host, "20", 1).run()
    assert info.value.code == "SANDBOX_TAINT_MISSING"
    assert [a[4] for a in host.cli] == ["configure"]
    assert not (cfg.evidence_trial("20", 1) / "summary.json").exists()
    assert not driver.trial_done(cfg, "20", 1)
    trial = json.loads((cfg.trial_dir("20", 1) / "trial.json").read_text())
    assert trial["status"] == "round_aborted"


def test_cluster_level_failures_abort_round(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    host = FakeHost([_body()])
    host.port_in_use = lambda port: port == cfg.ports.web
    with pytest.raises(driver.RoundAbort) as info:
        driver.Trial(cfg, host, "20", 1).run()
    assert info.value.code == "PORT_BUSY" and host.cli == []
    host = FakeHost([_body()])
    _failing_cli(host, "configure", 1)
    with pytest.raises(driver.RoundAbort) as info:
        driver.Trial(cfg, host, "23", 1).run()
    assert info.value.code == "ITBENCH_CONFIGURE_FAILED"
    assert not (cfg.evidence_trial("23", 1) / "summary.json").exists()


class CrashingForward(FakeHost):
    def _forward(self, *args: Any) -> Started:
        raise RuntimeError("synthetic")


def test_infra_failure_streak_aborts_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(driver, "prepare_round", lambda cfg, host: None)
    host = CrashingForward([_body()])
    with pytest.raises(driver.RoundAbort) as info:
        driver.run_round(cfg, host, ["20", "23"], [1])
    assert info.value.code == "INFRA_FAILURE_STREAK"
    for sid in ("20", "23"):
        summary = json.loads((cfg.evidence_trial(sid, 1) / "summary.json").read_text())
        assert summary["status"] == "crashed"


def test_killed_driver_unwinds_and_rerun_starts_fresh(tmp_path: Path) -> None:
    """A signal mid-trial runs the cleanup, records no summary; the rerun
    sets the old directory aside, so a fresh PG directory is created."""
    cfg = _cfg(tmp_path)
    host = FakeHost([_body()])
    real, partial = host.run, {}

    def killed_at_inject(argv: Sequence[str], **kwargs: Any) -> Any:
        if argv[4] == "inject":
            partial.update(json.loads(
                (cfg.trial_dir("20", 1) / "trial.json.partial").read_text()
            ))  # fmt: skip
            raise driver.DriverKilled(15)
        return real(argv, **kwargs)

    host.run = killed_at_inject
    with pytest.raises(driver.DriverKilled):
        driver.Trial(cfg, host, "20", 1).run()
    started = {i["name"]: i["pid"] for i in partial["started"]}
    assert started["port-forward:prometheus"] and "remove-faults" in started
    assert [a[4] for a in host.cli][-2:] == ["remove", "undeploy"]
    assert not (cfg.evidence_trial("20", 1) / "summary.json").exists()
    old = json.loads((cfg.trial_dir("20", 1) / "trial.json").read_text())
    assert old["status"] == "killed" and old["reason"] == "SIGNAL_SIGTERM"
    assert not (cfg.trial_dir("20", 1) / "trial.json.partial").exists()

    entry = _alert("RequestErrorRate", "2026-11-24T10:03:00Z", service_name="cart")
    host = FakeHost([_body(), _body(entry)])
    fresh = host.start_pg

    def start_pg(directory: Path, port: int) -> Any:
        assert not directory.exists(), "PG_DIRECTORY_EXISTS"
        return fresh(directory, port)

    host.start_pg = start_pg
    block = driver.Trial(cfg, host, "20", 1).run()
    assert block["status"] == "completed"
    (aside,) = cfg.trial_dir("20", 1).parent.glob("trial-1.aborted-*")
    assert (aside / "trial.json").is_file()
    assert f"PREVIOUS_ATTEMPT_SET_ASIDE:{aside.name}" in block["disclosures"]


def test_signal_handler_raises_once() -> None:
    import signal

    previous = driver.install_signal_handlers()
    try:
        with pytest.raises(driver.DriverKilled) as info:
            os.kill(os.getpid(), signal.SIGHUP)
            for _ in range(100):
                pass
        assert info.value.code == "SIGNAL_SIGHUP"
        os.kill(os.getpid(), signal.SIGTERM)  # ignored while unwinding
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


class DyingForward(FakeHost):
    """Every forward reports dead after ``lives`` liveness checks."""

    def __init__(self, *args: Any, lives: int, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.lives, self.forwards = lives, 0

    def _forward(self, ns: str, resource: str, local: int, *rest: Any) -> Started:
        self.forwards += 1
        checks = [0]

        def alive() -> bool:
            checks[0] += 1
            return "prometheus" not in resource or checks[0] <= self.lives

        return Started(f"pf:{local}", lambda: None, alive=alive)


def test_dead_port_forward_restarted_once_then_stops(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    entry = _alert("RequestErrorRate", "2026-11-24T10:03:00Z", service_name="cart")
    host = DyingForward([_body(), _body(), _body(entry)], lives=10)
    block = driver.Trial(cfg, host, "20", 1).run()
    assert block["status"] == "completed" and host.forwards == 2
    assert "PORT_FORWARD_RESTARTED:prometheus" not in block["disclosures"]

    host = DyingForward([_body()], lives=1)
    block = driver.Trial(cfg, host, "23", 1).run()
    assert "PORT_FORWARD_RESTARTED:prometheus" in block["disclosures"]
    assert block["status"] == "infra_failed" and block["reason"] == "PORT_FORWARD_DIED"
    assert host.forwards == 3


class UnreadableAlerts(FakeHost):
    def _get(self, url: str) -> tuple[int, Any]:
        if url.endswith("/api/v1/alerts") and len(self.alert_bodies) == 1:
            return 503, None
        return super()._get(url)


def test_alerts_poll_unreadable_disclosed_once(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    block = driver.Trial(cfg, UnreadableAlerts([_body(), _body()]), "20", 1).run()
    assert block["reason"] == "NO_ENTRY_ALERT" and block["alert_polls"] > 2
    assert block["disclosures"].count("ALERTS_POLL_UNREADABLE") == 1
    assert set(block["disk_before"]) == {"unit", "total", "used", "free"}


def test_post_deploy_hook_runs_before_identity(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    host = FakeHost([_body()])
    at: dict[str, Any] = {}

    class Hooked(driver.Trial):
        def _post_deploy(self) -> None:
            at["times"] = dict(self.block["times"])
            at["can_i"] = any(a[1:3] == ["auth", "can-i"] for a in host.calls)

    Hooked(cfg, host, "20", 1).run()
    assert "environment_ready" in at["times"] and at["can_i"] is False


def test_judge_subcommand_skips_scored_unless_forced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    entry = _alert("RequestErrorRate", "2026-11-24T10:03:00Z", service_name="cart")
    host = FakeHost([_body(), _body(entry)])
    assert driver.Trial(cfg, host, "20", 1).run()["status"] == "completed"
    assert driver.unjudged(cfg, "20", 1)
    monkeypatch.setattr(driver, "_config", lambda args: cfg)
    argv = ["judge", "--round", "unit", "--itbench", str(cfg.itbench)]
    assert driver.main(argv, host) == 0 and len(host.jobs) == 1
    assert driver.main(argv, host) == 0 and len(host.jobs) == 1
    assert driver.main([*argv, "--force"], host) == 0 and len(host.jobs) == 2
    # With judging on, an ok row that was not judged is judged again.
    cfg.judge = True
    path = cfg.evidence_trial("20", 1) / "scores.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "judged": False}))
    assert not driver.scored_ok(cfg, "20", 1)
    path.write_text(json.dumps({**json.loads(path.read_text()), "judged": True}))
    assert driver.scored_ok(cfg, "20", 1)


def test_run_judge_timeout_is_a_failed_try(monkeypatch: pytest.MonkeyPatch) -> None:
    def hung(argv: Sequence[str], **kwargs: Any) -> Any:
        assert kwargs["timeout"] == driver.JUDGE_PROCESS_TIMEOUT_S
        raise subprocess.TimeoutExpired(list(argv), kwargs["timeout"])

    monkeypatch.setattr(driver.upstream, "run_group", hung)
    job = {"ground_truth": "/g", "scenario": "20", "outputs": "/o", "trial": 1,
           "work_dir": "/w", "out": "/s", "judge": True}  # fmt: skip
    assert driver.run_judge(job) == 124


def test_judge_trial_evaluator_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.itbench_lite import official_eval

    gt_root = tmp_path / "gt"
    (gt_root / "20").mkdir(parents=True)
    (gt_root / "20" / "ground_truth.yaml").write_text("entities: []\n")
    trial = tmp_path / "outputs" / "20" / "1"
    (trial / "outputs").mkdir(parents=True)
    (trial / "outputs" / "agent_output.json").write_text(json.dumps(_output()))
    seen: dict[str, Any] = {}

    def run(*args: Any, **kwargs: Any) -> Path:
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(["evaluator"], kwargs["timeout"])

    monkeypatch.setattr(official_eval, "report_present", lambda d: True)
    monkeypatch.setattr(official_eval, "judge_config", lambda: SimpleNamespace())
    monkeypatch.setattr(official_eval, "evaluator_dir", lambda: tmp_path / "ev")
    monkeypatch.setattr(official_eval, "run", run)
    out = tmp_path / "scores.json"
    code = judge_trial.main([
        "--ground-truth", str(gt_root), "--scenario", "20",
        "--outputs", str(tmp_path / "outputs"), "--trial", "1",
        "--work-dir", str(tmp_path / "w"), "--out", str(out), "--judge",
    ])  # fmt: skip
    assert code == 3 and seen["timeout"] == judge_trial.JUDGE_TIMEOUT_S
    assert json.loads(out.read_text())["judge_error"] == "TimeoutExpired"


def test_run_group_kills_the_whole_group_on_timeout(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    script = f"sleep 30 & echo $! > {pidfile}; wait"
    with pytest.raises(subprocess.TimeoutExpired):
        upstream.run_group(["sh", "-c", script], timeout=1.0)
    child = int(pidfile.read_text())
    import time

    for _ in range(40):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("grandchild survived the timeout")


def test_configure_argv() -> None:
    assert upstream.cli_argv("configure", Path("/k"))[4:] == [
        "configure", "environment", "--kubeconfig", "/k",
    ]  # fmt: skip


def test_alert_timeline_codes() -> None:
    """#866: pre-fault sets, pending->firing, re-fire and new sets are counted."""
    pending = _alert(
        "A", "2026-11-24T10:00:00Z", state="pending", namespace="otel-demo"
    )
    firing = _alert("B", "2026-11-24T09:00:00Z", namespace="otel-demo")
    gone = _alert("C", "2026-11-24T09:00:00Z", namespace="otel-demo")
    timeline = alerts.AlertTimeline()
    timeline.record_prefault([pending, firing, gone], "t0")
    turned = dict(pending, state="firing")
    refired = dict(firing, activeAt="2026-11-24T10:10:00Z")
    new_pending = _alert(
        "D", "2026-11-24T10:11:00Z", state="pending", namespace="otel-demo"
    )
    new_watchdog = _alert("Watchdog", "2026-11-24T10:11:00Z")
    timeline.record_poll(1, "t1", [pending, firing])
    timeline.record_poll(2, "t2", [turned, refired, new_pending, new_watchdog])
    timeline.record_poll(3, "t3", [turned, refired, new_pending, new_watchdog])
    counts = timeline.counts()
    assert counts == {
        "prefault_sets": 3,
        "prefault_pending_sets": 1,
        "prefault_pending_turned_firing": 1,
        "prefault_sets_reactivated": 1,
        "prefault_sets_gone": 1,
        "new_sets_seen": 2,
        "new_sets_firing": 1,
        "new_sets_firing_baseline_name": 1,
        "new_sets_pending_only": 1,
        "polls": 3,
    }
    details = timeline.details()
    obs = [
        o
        for o in details["observations"]
        if o["labels"]["alertname"] == "A" and o["state"] == "firing"
    ]
    assert obs[0]["first_poll"] == 2 and obs[0]["last_poll"] == 3


def test_alert_range_counts() -> None:
    body = {"status": "success", "data": {"result": [
        {"metric": {"__name__": "ALERTS", "alertname": "X", "alertstate": "firing", "namespace": "n"},
         "values": [[1000.0, "1"], [1015.0, "1"]]},
        {"metric": {"__name__": "ALERTS", "alertname": "Y", "alertstate": "firing"},
         "values": [[900.0, "1"]]},
        {"metric": {"__name__": "ALERTS", "alertname": "Z", "alertstate": "pending"},
         "values": [[1000.0, "1"]]},
    ]}}  # fmt: skip
    series = alerts.range_series(body)
    assert len(series) == 3 and series[0]["samples"] == 2
    assert alerts.range_counts(series, {}, 950.0) == {
        "range_firing_series": 2,
        "range_firing_first_after_inject": 1,
        "range_firing_new_after_inject": 1,
    }


def test_no_entry_trial_writes_timeline_to_trial_json_only(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    noise = _alert(
        "KubePodNotReady",
        "2026-11-24T09:59:00Z",
        state="pending",
        namespace="otel-demo",
        pod="x-1",
    )
    host = FakeHost([_body(noise), _body(dict(noise, state="firing"))])
    block = driver.Trial(cfg, host, "23", 1).run()
    assert block["reason"] == "NO_ENTRY_ALERT"
    counts = block["alert_diag_counts"]
    assert (
        counts["prefault_pending_turned_firing"] == 1 and counts["new_sets_seen"] == 0
    )
    trial = json.loads((cfg.trial_dir("23", 1) / "trial.json").read_text())
    assert trial["alert_diag"]["prefault"][0]["labels"]["pod"] == "x-1"
    summary = (cfg.evidence_trial("23", 1) / "summary.json").read_text()
    assert "alert_diag_counts" in summary and "x-1" not in summary


def test_span_namespace_counts() -> None:
    body = {"status": "success", "data": {"result": [
        {"metric": {"namespace": "otel-demo", "service_name": "cart"}, "value": [1.0, "4"]},
        {"metric": {"service_name": "frontend"}, "value": [1.0, "2"]},
        {"metric": {"namespace": "other", "service_name": "x"}, "value": [1.0, "1"]},
    ]}}  # fmt: skip
    rows = alerts.span_namespace_rows(body)
    assert rows[1] == {"namespace": "", "service_name": "frontend", "count": "2"}
    assert alerts.span_namespace_counts(rows, "otel-demo") == {
        "span_metric_groups": 3,
        "span_metric_groups_with_namespace": 2,
        "span_metric_groups_app_namespace": 1,
    }
    assert alerts.span_namespace_counts(
        alerts.span_namespace_rows(None), "otel-demo"
    ) == {
        "span_metric_groups": 0,
        "span_metric_groups_with_namespace": 0,
        "span_metric_groups_app_namespace": 0,
    }


class SpanMetricsHost(FakeHost):
    def _get(self, url: str) -> tuple[int, Any]:
        if "/api/v1/query?" in url and "traces_span_metrics_calls_total" in url:
            return 200, {"status": "success", "data": {"result": [
                {"metric": {"service_name": "frontend"}, "value": [1.0, "3"]}]}}  # fmt: skip
        return super()._get(url)


def test_span_metric_counts_in_summary_rows_in_trial_json(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.scrape_namespace_patch = False
    host = SpanMetricsHost([_body()])
    block = driver.Trial(cfg, host, "30", 1).run()
    assert block["span_metric_counts"] == {
        "span_metric_groups": 1,
        "span_metric_groups_with_namespace": 0,
        "span_metric_groups_app_namespace": 0,
    }
    trial = json.loads((cfg.trial_dir("30", 1) / "trial.json").read_text())
    assert (
        trial["alert_diag"]["span_metrics_by_namespace"][0]["service_name"]
        == "frontend"
    )
    summary = (cfg.evidence_trial("30", 1) / "summary.json").read_text()
    assert (
        "span_metric_counts" in summary and "span_metrics_by_namespace" not in summary
    )


# -- #866 eval-side scrape-class patch -------------------------------------------------


def _patches(host: FakeHost) -> list[list[str]]:
    return [a for a in host.calls if a[3:5] == ["patch", "prometheus"]]


def test_scrape_patch_applied_settled_and_disclosed(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    entry = _alert("RequestErrorRate", "2026-11-24T10:09:00Z", service_name="cart")
    host = FakeHost([_body(), _body(entry)])
    block = driver.Trial(cfg, host, "20", 1).run()
    assert block["status"] == "completed", block
    (patch,) = _patches(host)
    assert patch[:6] == ["kubectl", "-n", "itbench-observability", "patch",
                         "prometheus", "kube-prometheus-stack-prometheus"]  # fmt: skip
    assert patch[6:8] == ["--type=merge", "-p"]
    assert json.loads(patch[8]) == {"spec": {"scrapeClasses": [{
        "name": "opspilot-eval-honor-namespace", "default": True,
        "metricRelabelings": [
            {"sourceLabels": ["job", "exported_namespace"],
             "regex": "itbench-otel-gateway-applications-collector;(.+)",
             "targetLabel": "namespace", "replacement": "$1", "action": "replace"},
            {"sourceLabels": ["job"], "regex": "itbench-otel-gateway-applications-collector",
             "targetLabel": "exported_namespace", "replacement": "", "action": "replace"},
        ]}]}}  # fmt: skip
    # The patch precedes OpsPilot's identity; the settle precedes the snapshot.
    first_can_i = next(
        i for i, a in enumerate(host.calls) if a[1:3] == ["auth", "can-i"]
    )
    assert host.calls.index(patch) < first_can_i
    assert host.sleeps[0] == (cfg.settle.total_seconds(), 0)
    summary = json.loads((cfg.evidence_trial("20", 1) / "summary.json").read_text())
    assert driver.SCRAPE_PATCH_DISCLOSURE in summary["itbench_live"]["disclosures"]


def test_scrape_patch_refused_when_scrape_classes_present(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    host = FakeHost([_body()])
    host.prometheus_cr = {"spec": {"scrapeClasses": [{"name": "upstream"}]}}
    block = driver.Trial(cfg, host, "20", 1).run()
    assert block["status"] == "infra_failed"
    assert block["reason"] == "SCRAPE_CLASSES_PRESENT"
    assert _patches(host) == [] and host.alert_reads == []
    assert driver.SCRAPE_PATCH_DISCLOSURE not in block["disclosures"]
    assert [a[4] for a in host.cli][-1] == "undeploy"


def test_scrape_patch_not_effective_stops_before_snapshot(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    host = FakeHost([_body()])
    host.span_body = {"status": "success", "data": {"result": [
        {"metric": {"service_name": "cart", "exported_namespace": "otel-demo"}, "value": [1.0, "4"]}]}}  # fmt: skip
    block = driver.Trial(cfg, host, "20", 1).run()
    assert block["status"] == "infra_failed"
    assert block["reason"] == "SCRAPE_PATCH_NOT_EFFECTIVE"
    assert host.alert_reads == [] and "inject" not in [a[4] for a in host.cli]
    summary = json.loads((cfg.evidence_trial("20", 1) / "summary.json").read_text())
    assert driver.SCRAPE_PATCH_DISCLOSURE in summary["itbench_live"]["disclosures"]


def test_scrape_patch_off_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(tmp_path)
    cfg.scrape_namespace_patch = False
    host = FakeHost([_body()])
    host.span_body = None
    block = driver.Trial(cfg, host, "20", 1).run()
    assert block["reason"] == "NO_ENTRY_ALERT"
    assert _patches(host) == [] and not any(
        a[3:5] == ["get", "prometheus"] for a in host.calls
    )
    assert host.sleeps[0][0] == cfg.poll_seconds  # no settle
    assert driver.SCRAPE_PATCH_DISCLOSURE not in block["disclosures"]
    args = SimpleNamespace(
        itbench=str(tmp_path), no_scrape_namespace_patch=True, settle_seconds=12.0
    )
    built = driver._config(args)
    assert built.scrape_namespace_patch is False and built.settle == timedelta(
        seconds=12
    )
    assert (
        driver._config(SimpleNamespace(itbench=str(tmp_path))).scrape_namespace_patch
        is True
    )


class DiesDuringSettle(FakeHost):
    """The first Prometheus forward is dead by the end of the settle wait."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.forwards, self.forwards_at_span_query = 0, None

    def _forward(self, ns: str, resource: str, local: int, *rest: Any) -> Started:
        self.forwards += 1
        first = self.forwards == 1
        return Started(f"pf:{local}", lambda: None, alive=lambda: not first)

    def _get(self, url: str) -> tuple[int, Any]:
        if "traces_span_metrics_calls_total" in url:
            self.forwards_at_span_query = self.forwards
        return super()._get(url)


def test_forwards_checked_after_settle(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    host = DiesDuringSettle([_body()])
    block = driver.Trial(cfg, host, "20", 1).run()
    assert block["reason"] == "NO_ENTRY_ALERT"
    assert "PORT_FORWARD_RESTARTED:prometheus" in block["disclosures"]
    assert host.forwards == 3 and host.forwards_at_span_query == 3
