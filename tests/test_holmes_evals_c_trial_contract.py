"""#666 PR-C contract: one C5 trial end to end with replaced Backends.

Independent contract tests written from the task record's "PR-C 接口"
section (the pass flow, ``Backends`` additions, new codes, summary fields),
without reading the PR-C implementation. PG, web, worker, judge are the
PR-B harness's recorders (``tests/test_holmes_evals_trial_contract.py``);
the question's Loki is an in-process ``FakeLoki`` reached through the real
``shift.LokiAdmin``; the fake log backend, gateways, VictoriaLogs container,
port-forwards and namespace checks are recorders. The upstream is synthetic
(answer isolation); every clock is fixed.

Covered: a C5-only round starts no Elasticsearch; one Loki pass (fake log
backend, cleanup + release check, ``before_test``, port-forward, whole-day
shift, delivery, stop order); environments of setup scripts, port-forward,
web and worker; the delivered and deshifted texts; the summary's new fields
and what it must not hold; the shift-day wait before setup and
``SHIFT_DAY_UNAVAILABLE`` retried after a wait; relative Loki (282) and
VictoriaLogs (258) passes; 258's ``DATA_WINDOW_EXCEEDED``; ``ENV_ISOLATION``
for a worker pointed past the gateway; ``LOG_BACKEND_NOT_RELEASED`` before a
pass and ``CLEANUP_FAILED`` after it; a busy question port; the Loki shift's
infrastructure and deterministic codes; gateway mode; a B1 trial's summary
gaining only ``data_plane``.

Assumptions beyond the interface (also marked ``# ASSUMPTION:``):

* ``Config`` takes ``loki_mode``/``victorialogs_mode``; ``Ports`` takes the
  four new port names; ``Backends`` takes the seven new fields by name.
* In fake mode the driver calls ``start_fake_log_backend(backend,
  backend_port, gateway_port, work_dir)`` once per pass and ``loki_admin``
  with some engineer-side URL; the harness routes every ``loki_admin`` to
  its own ``FakeLoki``.
* ``setup.released`` entries carry ``namespace_gone`` and ``busy_ports``
  (a list); ``setup.purged`` holds two empty lists for C5.
* A failed release check before the pass is retried in the same trial; the
  ``finally`` cleanup of that chance may or may not run ``after_test``.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts.itbench_lite.services import Started
from tests.holmes_evals_b_support import no_proxy_env  # noqa: F401 - fixture
from tests.holmes_evals_c_support import (
    LOKI_RO_PW,
    LOKI_RO_USER,
    PROMPT_FIXED,
    Q100A,
    Q101,
    Q102,
    Q258,
    Q282,
    VL_RO_PW,
    VL_RO_USER,
    FakeLoki,
    free_port,
    ns_of,
    served_loki,
)
from tests.holmes_evals_support import FIXTURES, mod, write_question
from tests.test_holmes_evals_trial_contract import (
    DISCLOSURES as B1_DISCLOSURES,
)
from tests.test_holmes_evals_trial_contract import (
    EXPECTED_SENTINEL,
    JUDGE,
    MODEL,
    PROMPT_275,
    Q275,
    RATIONALE_SENTINEL,
    SUMMARY_KEYS,
    Harness,
    _report,
    _seed_275,
)

pytestmark = pytest.mark.usefixtures("no_proxy_env")

DAY = 86400
K = (date(2026, 11, 19) - date(2025, 8, 2)).days
START = datetime(2026, 11, 19, 17, 0, 0, tzinfo=UTC)
PROMPT_100A = PROMPT_FIXED.format(ns="app-100a")
PROMPT_101_NO_TIME = (
    "The payment-api pod in namespace app-101 had issues. What happened?"
)
PROMPT_282 = "In Loki, count the error-level lines for checkout in namespace app-282."
PROMPT_102 = "Fetch me the logs for the inventory-service pod in namespace app-102"
PROMPT_258 = (
    "Which services have logs in the 'app-258' namespace in VictoriaLogs in the "
    "last 5 minutes? List them by name."
)
PF = {
    Q100A: {"namespace": "app-100a", "service": "loki", "local_port": 3110, "remote_port": 3100},
    Q101: {"namespace": "app-101", "service": "loki", "local_port": 3101, "remote_port": 3100},
    Q102: {"namespace": "app-102", "service": "loki", "local_port": 3102, "remote_port": 3100},
    Q282: {"namespace": "app-282", "service": "loki", "local_port": 10282, "remote_port": 3100},
}  # fmt: skip
NAMESPACE = {Q100A: "app-100a", Q101: "app-101", Q102: "app-102", Q282: "app-282"}
LABELS = {"app": "payment-api", "namespace": "app-100a"}
B1_RECORD_KEYS = {
    "applied", "reason", "seconds", "anchor", "original_min", "original_max",
    "shifted_min", "shifted_max", "indices", "store_size_before",
    "store_size_after", "question_rewrites", "time_only_phrases",
}  # fmt: skip
B1_PORT_KEYS = {"pg", "web", "es", "prometheus", "jaeger_query", "k8s"}


class C5Harness(Harness):
    """The PR-B fake host plus the C5 data plane."""

    def __init__(self, tmp_path: Path, start: datetime, loki_url: str, loki: FakeLoki):
        super().__init__(tmp_path, start)
        self.loki = loki
        self.loki_url = loki_url
        self.fake_log_calls: list[tuple[str, int, int]] = []
        self.gateway_starts: list[dict[str, Any]] = []
        self.switches: list[dict[str, Any]] = []
        self.forwards: list[dict[str, Any]] = []
        self.admin_urls: list[str] = []
        self.vl_starts: list[int] = []
        self.ns_present: dict[str, bool] = {}
        self.ns_checks: list[str] = []
        self.after_cleans = lambda: True
        self.before_delay = 0.0
        self.seed_mode = "fixed"  # or "live_only"
        self.product_url: str | None = None  # overrides the fake backend's URL
        self.busy: dict[int, int] = {}
        self.report_text = "checkout logged errors"
        self.before_tests = 0

    # -- setup scripts
    def run_script(self, script, *, operation, cwd, env, timeout_s, log_path):
        qid = Path(cwd).name
        if qid == Q275:
            return super().run_script(
                script, operation=operation, cwd=cwd, env=env,
                timeout_s=timeout_s, log_path=log_path,
            )  # fmt: skip
        self.scripts.append(
            {"operation": operation, "script": script, "cwd": Path(cwd),
             "env": dict(env), "timeout_s": timeout_s}
        )  # fmt: skip
        self.events.append(f"script:{operation}")
        started = self.now()
        ns = NAMESPACE.get(qid)
        if operation == "before_test":
            self.before_tests += 1
            if ns:
                self.ns_present[ns] = True
            self._seed(qid)
            self.clock += timedelta(seconds=self.before_delay)
        elif ns and self.after_cleans():
            self.ns_present[ns] = False
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(f"{operation} ok\n")
        ended = self.now()
        return mod("setup").ScriptResult(
            operation=operation, exit_code=0, timed_out=False, started_at=started,
            ended_at=ended, seconds=(ended - started).total_seconds(),
            log_sha256="0" * 64, skipped=not script,
        )  # fmt: skip

    def _seed(self, qid: str) -> None:
        if qid not in (Q100A, Q101):
            return
        labels = dict(LABELS, namespace=NAMESPACE[qid])
        if self.seed_mode == "fixed":
            t0 = datetime(2025, 8, 2, 12, 0, tzinfo=UTC)
            rows = []
            for h in range(49):
                m = t0 + timedelta(hours=h)
                stamp = m.strftime("%Y-%m-%dT%H:%M:%SZ")
                rows.append(
                    (
                        ns_of(m),
                        json.dumps(
                            {
                                "timestamp": stamp,
                                "level": "error",
                                "msg": f"refused {h}",
                            }
                        ),
                    )
                )
            self.loki.add(labels, rows)
        self.loki.add(
            dict(labels, kind="live"), [(ns_of(self.clock), "live line at setup")]
        )

    # -- data plane
    def start_fake_log_backend(self, backend, backend_port, gateway_port, work_dir):
        self.events.append(f"start_fake_log:{backend}")
        self.fake_log_calls.append((backend, backend_port, gateway_port))
        url = self.product_url or f"http://127.0.0.1:{gateway_port}"
        if backend == "loki":
            env = {"OPSPILOT_LOKI_URL": url, "OPSPILOT_LOKI_USERNAME": LOKI_RO_USER,
                   "OPSPILOT_LOKI_PASSWORD": LOKI_RO_PW}  # fmt: skip
        else:
            env = {"OPSPILOT_VICTORIALOGS_URL": url,
                   "OPSPILOT_VICTORIALOGS_USERNAME": VL_RO_USER,
                   "OPSPILOT_VICTORIALOGS_PASSWORD": VL_RO_PW}  # fmt: skip
        return self._rec(f"fake_log:{backend}"), env

    def start_gateway(self, backend, port, upstream, state_dir, out):
        self.events.append(f"start_gateway:{backend}")
        self.gateway_starts.append(
            {"backend": backend, "port": port, "upstream": str(upstream)}
        )
        prefix = "OPSPILOT_LOKI_" if backend == "loki" else "OPSPILOT_VICTORIALOGS_"
        user, pw = (
            (LOKI_RO_USER, LOKI_RO_PW) if backend == "loki" else (VL_RO_USER, VL_RO_PW)
        )
        env = {f"{prefix}URL": f"http://127.0.0.1:{port}", f"{prefix}USERNAME": user,
               f"{prefix}PASSWORD": pw}  # fmt: skip
        return self._rec(f"gateway:{backend}"), env

    def switch_gateway(self, backend, upstream, state_dir):
        self.events.append(f"switch_gateway:{backend}")
        self.switches.append({"backend": backend, "upstream": str(upstream)})

    def start_victorialogs(self, port, work_dir):
        self.events.append("start_victorialogs")
        self.vl_starts.append(port)
        return self._rec("victorialogs")

    def start_port_forward(self, forward, env, log_path):
        self.events.append("start:port_forward")
        self.forwards.append({"forward": dict(forward), "env": dict(env)})
        return self._rec("port_forward")

    def loki_admin(self, url):
        self.events.append("loki_admin")
        self.admin_urls.append(str(url))
        return mod("shift").LokiAdmin(self.loki_url)

    def namespace_gone(self, namespace, env):
        self.ns_checks.append(namespace)
        return not self.ns_present.get(namespace, False)

    def port_in_use(self, port: int) -> bool:
        left = self.busy.get(port, 0)
        if left > 0:
            self.busy[port] = left - 1
            return True
        return False

    def _rec(self, name: str) -> Started:
        def stop() -> None:
            self.events.append(f"stop:{name}")

        return Started(name=name, _stop=stop, info={"pid": 0})

    # -- the Run
    def collect(self, dsn: str, run_id: str) -> tuple[dict, dict]:
        block, ledger = super().collect(dsn, run_id)
        block["report"] = _report(self.report_text)
        return block, ledger

    def backends(self) -> Any:
        return dataclasses.replace(
            super().backends(),
            start_gateway=self.start_gateway,
            switch_gateway=self.switch_gateway,
            start_victorialogs=self.start_victorialogs,
            start_fake_log_backend=self.start_fake_log_backend,
            start_port_forward=self.start_port_forward,
            loki_admin=self.loki_admin,
            namespace_gone=self.namespace_gone,
        )

    def secrets(self) -> list[str]:
        return [*super().secrets(), LOKI_RO_PW, VL_RO_PW]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    questions, driver = mod("questions"), mod("driver")
    upstream = tmp_path / "upstream"
    docs = {
        Q100A: {"user_prompt": PROMPT_100A, "port_forwards": [PF[Q100A]], "setup_timeout": 600},
        Q101: {"user_prompt": PROMPT_101_NO_TIME, "port_forwards": [PF[Q101]]},
        Q102: {"user_prompt": PROMPT_102, "port_forwards": [PF[Q102]]},
        Q282: {"user_prompt": PROMPT_282, "port_forwards": [PF[Q282]]},
        Q258: {"user_prompt": PROMPT_258, "setup_timeout": 120},
        Q275: {"user_prompt": PROMPT_275},
    }  # fmt: skip
    for qid, doc in docs.items():
        write_question(
            upstream,
            qid,
            {**doc, "before_test": "echo seed", "after_test": "echo clean",
             "expected_output": [EXPECTED_SENTINEL]},
        )  # fmt: skip
    monkeypatch.setattr(questions, "verify_upstream", lambda root: None)
    if hasattr(driver, "verify_upstream"):
        monkeypatch.setattr(driver, "verify_upstream", lambda root: None)
    monkeypatch.setenv("OPSPILOT_LEDGER_DIR", str(tmp_path / "ledgers"))
    monkeypatch.setenv("ZAI_API_KEY", JUDGE)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    env_file = tmp_path / "secrets.env"
    env_file.write_text(f"DEEPSEEK_API_KEY={MODEL}\n")
    env_file.chmod(0o600)
    kube = tmp_path / "engineer.kubeconfig"
    kube.write_text("apiVersion: v1\nkind: Config\n")
    loki = FakeLoki()
    made: list[Any] = []

    with served_loki(loki) as loki_url:

        def make(start: datetime, qids: tuple[str, ...], **over: Any):
            h = C5Harness(tmp_path, start, loki_url, loki)
            ports = dataclasses.replace(
                driver.Ports(), es=free_port(), loki_fake=free_port(),
                loki_gateway=free_port(), victorialogs=free_port(),
                victorialogs_gateway=free_port(),
            )  # fmt: skip
            cfg = driver.Config(
                **{
                    "round": "t1", "upstream": upstream, "questions": qids,
                    "ports": ports, "es_mode": "fake", "loki_mode": "fake",
                    "victorialogs_mode": "fake", "kubeconfig": kube,
                    "env_file": env_file, "trace": "off", "judge": True,
                    "allow_dirty": True, "tmp_root": tmp_path / "tmp",
                    "evidence_root": tmp_path / "evidence", "poll_seconds": 0.0,
                    **over,
                }
            )  # fmt: skip
            round_ = driver.Round()
            driver.prepare_round(cfg, h.backends(), round_)
            made.append(round_)
            return h, cfg, round_

        yield make
        for round_ in made:
            round_.close()


def _load(cfg: Any, qid: str) -> Any:
    return mod("questions").load_question(Path(cfg.upstream), qid)


def _summary(cfg: Any, qid: str, n: int = 1) -> tuple[dict, str]:
    path = Path(cfg.evidence_dir) / qid / f"trial-{n}" / "summary.json"
    raw = path.read_text(encoding="utf-8")
    return json.loads(raw), raw


def _no_secrets(h: C5Harness, *texts: str) -> None:
    for text in texts:
        for secret in h.secrets():
            assert secret not in text, secret[:6]


def _clean_setup_env(env: dict[str, str], cfg: Any) -> None:
    assert env["TZ"] == "UTC"
    assert not [k for k in env if k.startswith(("ELASTICSEARCH_", "OPSPILOT_"))]
    for name in ("DEEPSEEK_API_KEY", "ZAI_API_KEY", "GLM_API_KEY"):
        assert name not in env
    assert LOKI_RO_PW not in json.dumps(env) and VL_RO_PW not in json.dumps(env)


def _waited_before_setup(h: C5Harness) -> float:
    first = next(i for i, e in enumerate(h.events) if e.startswith("script:"))
    return sum(
        float(e.split(":", 1)[1]) for e in h.events[:first] if e.startswith("sleep:")
    )


# -- the fixed-time Loki question, fully ------------------------------------------------


def test_loki_fixed_time_trial_end_to_end(world) -> None:
    driver, shift = mod("driver"), mod("shift")
    h, cfg, _ = world(START, (Q100A,))
    assert "start_fake_es" not in h.events  # no B1 question: no ES
    assert "readonly_identity" not in h.events
    h.report_text = "payment-api refused connections on November 19, 2026 at 13:50 UTC."
    record = driver.run_trial(cfg, h.backends(), Q100A, _load(cfg, Q100A), 1)
    assert record["status"] == "completed"
    ev = h.events
    first = ev.index

    # The fake log backend: this pass, its own two ports.
    assert h.fake_log_calls == [("loki", cfg.ports.loki_fake, cfg.ports.loki_gateway)]
    assert "start_gateway:loki" not in ev and "switch_gateway:loki" not in ev
    ops = [s["operation"] for s in h.scripts]
    assert ops[:2] == ["after_test", "before_test"] and ops[-1] == "after_test"
    assert first("start_fake_log:loki") < first("script:after_test")
    assert first("script:before_test") < first("start:port_forward") < first("submit")
    assert first("loki_admin") < first("submit")
    last_after = len(ev) - 1 - ev[::-1].index("script:after_test")
    assert ev.index("stop:port_forward") < last_after
    assert last_after < ev.index("stop:fake_log:loki")
    assert "app-100a" in h.ns_checks

    # Setup scripts and the port-forward: engineer kubeconfig, TZ=UTC only.
    for s in h.scripts:
        assert s["cwd"] == Path(cfg.upstream) / FIXTURES / Q100A
        assert s["timeout_s"] == 600
        _clean_setup_env(s["env"], cfg)
        assert s["env"]["KUBECONFIG"] == str(cfg.kubeconfig)
        assert "VICTORIALOGS_URL" not in s["env"]
    (fw,) = h.forwards
    assert fw["forward"] == PF[Q100A]
    assert fw["env"]["KUBECONFIG"] == str(cfg.kubeconfig)
    assert not [k for k in fw["env"] if k.startswith(("OPSPILOT_", "ELASTICSEARCH_"))]

    # Product processes: only the gateway address and the read-only account.
    (web,) = h.web_envs
    (worker,) = h.worker_envs
    gateway = f"http://127.0.0.1:{cfg.ports.loki_gateway}"
    for env in (web, worker):
        assert not [k for k in env if k.startswith(("OPSPILOT_ES_", "ELASTICSEARCH_"))]
        assert not [k for k in env if k.startswith("OPSPILOT_VICTORIALOGS_")]
        assert "VICTORIALOGS_URL" not in env
        assert env["OPSPILOT_LOKI_URL"].rstrip("/") == gateway
    assert [k for k in web if k.startswith("OPSPILOT_LOKI_")] == ["OPSPILOT_LOKI_URL"]
    assert worker["OPSPILOT_LOKI_USERNAME"] == LOKI_RO_USER
    assert worker["OPSPILOT_LOKI_PASSWORD"] == LOKI_RO_PW
    assert LOKI_RO_PW not in json.dumps(web)

    # Whole-day shift: question, data and judge text.
    (sub,) = h.submits
    assert sub["question"] == shift.rewrite_question(PROMPT_100A, K * DAY)[0]
    assert "November 19, 2026 around 13:45 UTC" in sub["question"]
    pushed = [
        (ns, line) for ns, line in h.loki.entries(LABELS)
        if ns >= ns_of(datetime(2026, 1, 1, tzinfo=UTC))
    ]  # fmt: skip
    assert pushed and len(pushed) < 49  # future entries dropped
    assert max(ns for ns, _ in pushed) <= ns_of(START + timedelta(minutes=5))
    assert (
        len(
            [
                1
                for ns, _ in h.loki.entries(LABELS)
                if ns < ns_of(datetime(2026, 1, 1, tzinfo=UTC))
            ]
        )
        == 49
    )
    (text,) = h.judge_texts
    assert "August 2, 2025 at 13:50 UTC" in text
    assert "November 19, 2026" not in text

    # Summary.
    summary, raw = _summary(cfg, Q100A)
    he = summary["holmes_eval"]
    dp = he["data_plane"]
    assert dp["backend"] == "loki" and dp["mode"] == "fake"
    assert dp["product_url"].rstrip("/") == gateway
    assert dp["port_forwards"] == [PF[Q100A]]
    assert "gateway_upstream" in dp
    sh = he["shift"]
    assert sh["applied"] is True and sh["backend"] == "loki"
    assert sh["seconds"] == K * DAY
    assert sh["anchor_source"] == "question"
    assert sh["pushed"] > 0 and sh["dropped_future"] > 0
    released = he["setup"]["released"]
    for phase in ("before", "after"):
        assert released[phase]["namespace_gone"] is True
        assert list(released[phase]["busy_ports"]) == []
    purged = he["setup"]["purged"]
    assert len(purged) == 2 and all(list(v) == [] for v in purged.values())
    written = he["delivery"]["data_written_to_delivery_seconds"]
    assert isinstance(written, (int, float)) and written >= 0
    codes = set(he["disclosures"])
    assert {"LOKI_ORIGINALS_KEPT", "LOKI_FUTURE_DROPPED", "LOG_LINE_TIME_REWRITTEN",
            "SHIFT_WHOLE_DAYS"} <= codes  # fmt: skip
    assert not codes & {"DESHIFT_TIME_ONLY_UNCHANGED", "DESHIFT_DATE_MIDPOINT"}
    assert "K8S_TOOLS_UPSTREAM_DISABLED" not in codes
    assert "SKILL_ASSERTION_DROPPED" not in codes
    assert he["judge"]["choice"] == "A" and he["category"] == "ok"
    for forbidden in (EXPECTED_SENTINEL, RATIONALE_SENTINEL, text):
        assert forbidden not in raw
    _no_secrets(h, raw)
    assert driver.trial_state(cfg, Q100A, 1) == "done"


# -- shift-day waiting ----------------------------------------------------------------------


def test_waits_for_a_feasible_time_of_day_before_setup(world) -> None:
    driver = mod("driver")
    h, cfg, _ = world(datetime(2026, 11, 19, 14, 0, tzinfo=UTC), (Q100A,))
    record = driver.run_trial(cfg, h.backends(), Q100A, _load(cfg, Q100A), 1)
    assert record["status"] == "completed"
    waited = _waited_before_setup(h)
    assert waited >= 46 * 60 - 1  # until today's 13:45 hour + LEAD is past
    he = _summary(cfg, Q100A)[0]["holmes_eval"]
    assert he["shift_day_wait_seconds"] == pytest.approx(waited, abs=1)
    assert he["shift"]["seconds"] == K * DAY
    assert "November 19, 2026 around 13:45 UTC" in h.submits[0]["question"]


def test_shift_day_unavailable_after_slow_setup_is_retried_after_waiting(world) -> None:
    driver = mod("driver")
    h, cfg, _ = world(datetime(2026, 11, 19, 13, 20, tzinfo=UTC), (Q100A,))
    h.before_delay = 15 * 60  # setup runs past the estimate into the window
    record = driver.run_trial(cfg, h.backends(), Q100A, _load(cfg, Q100A), 1)
    assert record["status"] == "completed"
    first, *_ = record["history"]
    assert first["code"] == "SHIFT_DAY_UNAVAILABLE" and first["kind"] == "infra"
    he = _summary(cfg, Q100A)[0]["holmes_eval"]
    assert he["chance"] >= 2
    assert he["shift_day_wait_seconds"] > 0
    assert he["shift"]["seconds"] == K * DAY
    assert len(h.submits) == 1


# -- relative questions -----------------------------------------------------------------------


def test_relative_loki_282_is_not_shifted_and_drops_skill_assertion(world) -> None:
    driver = mod("driver")
    h, cfg, _ = world(START, (Q282,))
    record = driver.run_trial(cfg, h.backends(), Q282, _load(cfg, Q282), 1)
    assert record["status"] == "completed"
    (sub,) = h.submits
    assert sub["question"] == PROMPT_282
    assert not [r for r in h.loki.requests if r["path"].endswith("/push")]
    (fw,) = h.forwards
    assert fw["forward"] == PF[Q282]
    he = _summary(cfg, Q282)[0]["holmes_eval"]
    assert he["shift"]["applied"] is False
    assert he["shift"]["reason"] == "RELATIVE_TIME"
    assert "SKILL_ASSERTION_DROPPED" in he["disclosures"]
    assert "SHIFT_WHOLE_DAYS" not in he["disclosures"]
    assert he["data_plane"]["backend"] == "loki"


def test_victorialogs_258_pass(world) -> None:
    driver = mod("driver")
    h, cfg, _ = world(START, (Q258,))
    h.before_delay = 10.0
    record = driver.run_trial(cfg, h.backends(), Q258, _load(cfg, Q258), 1)
    assert record["status"] == "completed"
    ((backend, backend_port, gateway_port),) = h.fake_log_calls
    assert backend == "victorialogs"
    assert (backend_port, gateway_port) == (
        cfg.ports.victorialogs,
        cfg.ports.victorialogs_gateway,
    )
    assert h.forwards == []
    for s in h.scripts:
        _clean_setup_env(s["env"], cfg)
        url = s["env"]["VICTORIALOGS_URL"].rstrip("/")
        assert url.endswith(f":{backend_port}")  # the backend, not the gateway
    (worker,) = h.worker_envs
    (web,) = h.web_envs
    for env in (web, worker):
        assert "VICTORIALOGS_URL" not in env
        assert not [k for k in env if k.startswith(("OPSPILOT_ES_", "OPSPILOT_LOKI_"))]
        assert env["OPSPILOT_VICTORIALOGS_URL"].rstrip("/").endswith(f":{gateway_port}")
    assert worker["OPSPILOT_VICTORIALOGS_PASSWORD"] == VL_RO_PW
    assert [k for k in web if k.startswith("OPSPILOT_VICTORIALOGS_")] == [
        "OPSPILOT_VICTORIALOGS_URL"
    ]
    assert h.submits[0]["question"] == PROMPT_258
    summary, raw = _summary(cfg, Q258)
    he = summary["holmes_eval"]
    assert he["data_plane"]["backend"] == "victorialogs"
    assert he["data_plane"]["mode"] == "fake"
    assert he["shift"]["applied"] is False and he["shift"]["reason"] == "RELATIVE_TIME"
    assert "K8S_TOOLS_UPSTREAM_DISABLED" in he["disclosures"]
    written = he["delivery"]["data_written_to_delivery_seconds"]
    assert 10 <= written <= 240
    _no_secrets(h, raw)


def test_258_data_window_exceeded_voids_the_attempt(world) -> None:
    driver = mod("driver")
    h, cfg, _ = world(START, (Q258,))
    h.before_delay = 200.0  # before_test start -> delivery ~260 s > 240 s
    h.worker_delay = 60.0  # setup end -> delivery ~60 s, inside the 120 s line
    record = driver.run_trial(cfg, h.backends(), Q258, _load(cfg, Q258), 1)
    assert record["status"] == "missing"
    assert h.submits == []
    summary, raw = _summary(cfg, Q258)
    assert summary["reason"] == "CHANCES_EXHAUSTED"
    history = summary["holmes_eval"]["history"]
    assert len(history) == 3
    assert {e["code"] for e in history} == {"DATA_WINDOW_EXCEEDED"}
    assert {e["kind"] for e in history} == {"void"}
    _no_secrets(h, raw)


# -- isolation ------------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["backend_port", "question_address"])
def test_worker_pointed_past_the_gateway_aborts_the_round(world, target: str) -> None:
    driver = mod("driver")
    h, cfg, _ = world(START, (Q100A,))
    h.product_url = (
        f"http://127.0.0.1:{cfg.ports.loki_fake}"
        if target == "backend_port"
        else "http://127.0.0.1:3110"
    )
    with pytest.raises(driver.RoundAbort) as caught:
        driver.run_trial(cfg, h.backends(), Q100A, _load(cfg, Q100A), 1)
    assert caught.value.code == "ENV_ISOLATION"
    assert LOKI_RO_PW not in str(caught.value)
    assert h.submits == []
    assert "start:worker" not in h.events


# -- release checks and ports ---------------------------------------------------------------


def test_leftover_namespace_before_the_pass_is_retried(world) -> None:
    driver = mod("driver")
    h, cfg, _ = world(START, (Q282,))
    h.ns_present["app-282"] = True  # an earlier trial's leftover
    calls = {"n": 0}

    def cleans() -> bool:
        calls["n"] += 1
        return calls["n"] >= 2  # the first after_test leaves it behind

    h.after_cleans = cleans
    record = driver.run_trial(cfg, h.backends(), Q282, _load(cfg, Q282), 1)
    assert record["status"] == "completed"
    first, *_ = record["history"]
    assert first["code"] == "LOG_BACKEND_NOT_RELEASED" and first["kind"] == "infra"
    assert sum(h.sleeps) >= 150  # waited for the namespace (up to 180 s)
    assert len(h.submits) == 1


def test_namespace_left_after_the_pass_aborts_cleanup(world) -> None:
    driver = mod("driver")
    h, cfg, _ = world(START, (Q282,))
    h.after_cleans = lambda: h.before_tests == 0  # cleans only before setup
    with pytest.raises(driver.RoundAbort) as caught:
        driver.run_trial(cfg, h.backends(), Q282, _load(cfg, Q282), 1)
    assert caught.value.code == "CLEANUP_FAILED"


def test_busy_question_port_is_port_busy_and_retried(world) -> None:
    driver = mod("driver")
    h, cfg, _ = world(START, (Q100A,))
    h.busy[3110] = 1
    record = driver.run_trial(cfg, h.backends(), Q100A, _load(cfg, Q100A), 1)
    assert record["status"] == "completed"
    first, *_ = record["history"]
    assert first["code"] == "PORT_BUSY" and first["kind"] == "infra"
    assert "question:3110" in first["detail"]


# -- Loki shift codes ------------------------------------------------------------------------


def test_no_fixed_entries_is_infra_and_exhausts_chances(world) -> None:
    driver = mod("driver")
    h, cfg, _ = world(START, (Q100A,))
    h.seed_mode = "live_only"
    record = driver.run_trial(cfg, h.backends(), Q100A, _load(cfg, Q100A), 1)
    assert record["status"] == "missing"
    history = _summary(cfg, Q100A)[0]["holmes_eval"]["history"]
    assert len(history) == 3
    assert {e["code"] for e in history} == {"SHIFT_NO_FIXED_ENTRIES"}
    assert {e["kind"] for e in history} == {"infra"}
    assert h.submits == []


def test_question_without_time_is_deterministic(world) -> None:
    driver = mod("driver")
    h, cfg, _ = world(START, (Q101,))
    driver.run_question(cfg, h.backends(), Q101, [1, 2, 3])
    assert h.submits == []
    assert h.before_tests <= 1
    for n in (1, 2, 3):
        summary, raw = _summary(cfg, Q101, n)
        assert summary["status"] == "missing"
        assert summary["reason"] == "SHIFT_NO_QUESTION_TIME"
        assert driver.trial_state(cfg, Q101, n) == "missing"
        _no_secrets(h, raw)


# -- gateway mode ----------------------------------------------------------------------------


def test_gateway_mode_one_gateway_switched_per_question(world) -> None:
    driver = mod("driver")
    h, cfg, round_ = world(START, (Q100A, Q102), loki_mode="gateway")
    assert len(h.gateway_starts) == 1
    (gw,) = h.gateway_starts
    assert gw["backend"] == "loki" and gw["port"] == cfg.ports.loki_gateway
    assert "3110" in gw["upstream"]  # the first question's own Loki
    for qid in (Q100A, Q102):
        record = driver.run_trial(cfg, h.backends(), qid, _load(cfg, qid), 1)
        assert record["status"] == "completed", record
    assert h.fake_log_calls == []
    assert [s["backend"] for s in h.switches] == ["loki", "loki"]
    assert "3110" in h.switches[0]["upstream"] and "3102" in h.switches[1]["upstream"]
    submits = [i for i, e in enumerate(h.events) if e == "submit"]
    switches = [i for i, e in enumerate(h.events) if e == "switch_gateway:loki"]
    assert switches[0] < submits[0] < switches[1] < submits[1]
    urls = {w["OPSPILOT_LOKI_URL"].rstrip("/") for w in h.worker_envs}
    assert urls == {f"http://127.0.0.1:{cfg.ports.loki_gateway}"}
    he = _summary(cfg, Q100A)[0]["holmes_eval"]
    assert he["data_plane"]["mode"] == "gateway"
    assert "3110" in str(he["data_plane"]["gateway_upstream"])
    assert "stop:gateway:loki" not in h.events
    round_.close()
    assert "stop:gateway:loki" in h.events


# -- B1 summary bytes ---------------------------------------------------------------------------


def test_b1_trial_summary_gains_only_data_plane(world) -> None:
    driver = mod("driver")
    h, cfg, _ = world(datetime(2026, 11, 18, 10, 0, tzinfo=UTC), (Q275,))
    _seed_275(h)
    record = driver.run_trial(cfg, h.backends(), Q275, _load(cfg, Q275), 1)
    assert record["status"] == "completed"
    summary, raw = _summary(cfg, Q275)
    he = summary["holmes_eval"]
    assert set(he) == SUMMARY_KEYS | {"shift_timing", "data_plane"}
    assert set(he["ports"]) == B1_PORT_KEYS
    assert set(he["shift"]) == B1_RECORD_KEYS
    assert "released" not in he["setup"]
    assert "data_written_to_delivery_seconds" not in he["delivery"]
    assert set(he["disclosures"]) <= B1_DISCLOSURES
    assert he["data_plane"]["backend"] == "elasticsearch"
    assert he["data_plane"]["mode"] == "fake"
    assert h.fake_log_calls == [] and h.forwards == []
    (worker,) = h.worker_envs
    assert not [k for k in worker if "LOKI" in k or "VICTORIALOGS" in k]
    _no_secrets(h, raw)
