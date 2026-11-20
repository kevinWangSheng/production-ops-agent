"""#666 PR-C contract: C5 question lists, per-question log backends, ports,
config, process environments and the fake log backend (no running stack).

Independent contract tests written from the task record's "PR-C 接口"
section (docs/tasks/2026-11-18-holmes-evals-666.md) without reading the
PR-C implementation. No Loki, VictoriaLogs, docker, kind, model or network.

Rules under test:

* ``C5_LOKI_QUESTIONS`` (7), ``C5_VICTORIALOGS_QUESTIONS`` (2), ``C5_QUESTIONS``
  and ``ALL_QUESTIONS = B1 + C5``; the B1 sets are unchanged;
* ``resolve_questions``: empty -> the 30 B1 questions, ``b1``/``c5`` group
  names, numbers or full names over ``ALL_QUESTIONS``, ``259`` ambiguous ->
  ``QUESTION_AMBIGUOUS``, unknown -> ``QUESTION_UNKNOWN``;
* ``log_backends.LOG_QUESTIONS``/``backend_of``/``VICTORIALOGS_IMAGE``;
* new ``Ports`` defaults, ``per_attempt()`` unchanged, ``log_ports``;
* new ``Config`` fields and defaults, product log credentials not in repr;
* ``tool_env``/``web_env``/``worker_env`` byte-identical for B1 defaults;
  Loki questions only ``OPSPILOT_LOKI_*``, VL questions only
  ``OPSPILOT_VICTORIALOGS_*``, web only the ``_URL``;
* ``log_setup_env``: base variables + ``TZ=UTC`` (+ ``VICTORIALOGS_URL`` for
  VL, + ``KUBECONFIG`` when given) and nothing else;
* ``check_env_isolation`` new ``ENV_ISOLATION`` conditions;
* the default ``start_fake_log_backend``: one process, two ports -- the
  backend port is open and writable, the gateway port wants the read-only
  account (401 without), forwards only read paths (403 otherwise);
* the command line accepts ``--loki``, ``--victorialogs`` and the four new
  ``--<name>-port`` flags.

Assumptions beyond the interface (also marked ``# ASSUMPTION:``):

* ``log_ports`` is a ``Ports`` method (it is listed under "Ports 新增").
* ``log_setup_env`` may live in ``setup``, ``log_backends`` or ``driver``.
* ``check_env_isolation`` keeps its PR-B signature ``(env, engineer_key, *,
  role)``; the "worker URL points at the question backend" condition is
  tested end to end in ``test_holmes_evals_c_trial_contract.py`` because
  the interface does not say how the function learns the gateway port.
* In ``fake`` mode the product's ``OPSPILOT_<B>_URL`` is
  ``http://127.0.0.1:<gateway port>`` and the returned credentials are
  either ``_USERNAME``/``_PASSWORD`` (basic) or ``_BEARER_TOKEN``.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.holmes_evals_b_support import ENGINEER, no_proxy_env  # noqa: F401
from tests.holmes_evals_c_support import (
    C5_ALL,
    C5_LOKI,
    C5_VL,
    LOKI_RO_PW,
    Q100A,
    Q101,
    Q102,
    Q102A,
    Q102B,
    Q257,
    Q258,
    Q259B,
    Q259L,
    Q282,
    find,
    free_port,
    http,
)
from tests.holmes_evals_support import mod

pytestmark = pytest.mark.usefixtures("no_proxy_env")

REPO = Path(__file__).resolve().parents[1]
BASE = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/x",
    "TMPDIR": "/tmp/x",
    "LANG": "C.UTF-8",
    "LC_ALL": "C",
}
NOISY = {
    **BASE,
    "TZ": "Asia/Shanghai",
    "ELASTICSEARCH_URL": "http://127.0.0.1:9200",
    "ELASTICSEARCH_API_KEY": ENGINEER,
    "DEEPSEEK_API_KEY": "model-fake-value-4",
    "ZAI_API_KEY": "judge-fake-value-5",
    "OPSPILOT_LOKI_PASSWORD": LOKI_RO_PW,
    "OPSPILOT_ES_API_KEY": "readonly-fake-value-3",
    "LANGSMITH_API_KEY": "ls-fake-value-7",
    "VICTORIALOGS_URL": "http://elsewhere:9428",
    "http_proxy": "http://127.0.0.1:7890",
}
GW_LOKI = 29231
GW_VL = 29233


def _base(environ: dict[str, str]) -> dict[str, str]:
    return {
        k: v
        for k, v in environ.items()
        if k in {"PATH", "HOME", "TMPDIR", "LANG", "TZ"} or k.startswith("LC_")
    }


def _cfg(tmp_path: Path, **overrides: Any) -> Any:
    driver = mod("driver")
    wanted: dict[str, Any] = {
        "round": "t1",
        "upstream": tmp_path / "upstream",
        "questions": ("275_es_field_name_correction",),
        "es_mode": "fake",
        "es_url": "http://127.0.0.1:29292",
        "trace": "off",
        "judge": False,
        "allow_dirty": True,
        "tmp_root": tmp_path / "tmp",
        "evidence_root": tmp_path / "evidence",
        **overrides,
    }
    return driver.Config(**wanted)


def _pg() -> SimpleNamespace:
    return SimpleNamespace(
        owner_dsn="postgresql://owner:pw@127.0.0.1:29210/opspilot",
        runtime_dsn="postgresql://eval_worker:pw@127.0.0.1:29210/opspilot",
        web_dsn="postgresql://eval_web:pw@127.0.0.1:29210/opspilot",
        port=29210,
    )


def _k8s(tmp_path: Path) -> Any:
    ca = tmp_path / "ca.pem"
    ca.write_text("not a real ca\n")
    return mod("driver").K8sTarget(
        api_url="https://127.0.0.1:29243",
        ca_file=ca,
        token="k8s-fake-value-6",
        namespace="otel-demo",
    )


LOKI_ENV = {
    "OPSPILOT_LOKI_URL": f"http://127.0.0.1:{GW_LOKI}",
    "OPSPILOT_LOKI_USERNAME": "loki-ro-fake-user",
    "OPSPILOT_LOKI_PASSWORD": LOKI_RO_PW,
}
VL_ENV = {
    "OPSPILOT_VICTORIALOGS_URL": f"http://127.0.0.1:{GW_VL}",
    "OPSPILOT_VICTORIALOGS_USERNAME": "vl-ro-fake-user",
    "OPSPILOT_VICTORIALOGS_PASSWORD": "vl-ro-fake-value-12",
}


# -- question lists ------------------------------------------------------------------


def test_c5_question_lists() -> None:
    q = mod("questions")
    assert tuple(q.C5_LOKI_QUESTIONS) == C5_LOKI
    assert tuple(q.C5_VICTORIALOGS_QUESTIONS) == C5_VL
    assert tuple(q.C5_QUESTIONS) == C5_ALL
    assert len(q.C5_QUESTIONS) == 9
    # PR-D (lead ruling): ALL_QUESTIONS gains the six C2 questions after C5.
    assert tuple(q.ALL_QUESTIONS) == (
        tuple(q.B1_QUESTIONS) + C5_ALL + tuple(q.C2_QUESTIONS)
    )
    assert len(q.B1_QUESTIONS) == 30
    assert not set(q.B1_QUESTIONS) & set(q.C5_QUESTIONS)


def test_b1_sets_unchanged_and_r8_13_has_six() -> None:
    q = mod("questions")
    assert set(q.SKILL_ASSERTION_DROPPED) <= set(q.B1_QUESTIONS)
    assert len(q.SKILL_ASSERTION_DROPPED) == 5
    assert set(q.MULTI_CLUSTER) <= set(q.B1_QUESTIONS)
    assert len(q.MULTI_CLUSTER) == 10
    assert q.R8_13_SKILL_ASSERTION_DROPPED == set(q.SKILL_ASSERTION_DROPPED) | {Q282}
    assert set(mod("shift").FIXED_TIME) <= set(q.B1_QUESTIONS)
    assert len(mod("shift").FIXED_TIME) == 18


def _abort(fn: Any, *args: Any) -> str:
    driver = mod("driver")
    with pytest.raises(driver.RoundAbort) as caught:
        fn(*args)
    return str(caught.value.code)


@pytest.mark.parametrize("spec", [None, ""])
def test_resolve_default_is_b1(spec: str | None) -> None:
    q = mod("questions")
    assert mod("driver").resolve_questions(spec) == tuple(q.B1_QUESTIONS)


def test_resolve_group_names() -> None:
    q, driver = mod("questions"), mod("driver")
    assert driver.resolve_questions("b1") == tuple(q.B1_QUESTIONS)
    assert driver.resolve_questions("c5") == C5_ALL


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("100a", (Q100A,)),
        ("102,102a,102b", (Q102, Q102A, Q102B)),
        ("257,258", (Q257, Q258)),
        (Q259L, (Q259L,)),
        (Q259B, (Q259B,)),
        ("282", (Q282,)),
        ("275,101", ("275_es_field_name_correction", Q101)),
    ],
)
def test_resolve_numbers_and_names_over_all(
    spec: str, expected: tuple[str, ...]
) -> None:
    assert mod("driver").resolve_questions(spec) == expected


def test_resolve_259_is_ambiguous() -> None:
    assert _abort(mod("driver").resolve_questions, "259") == "QUESTION_AMBIGUOUS"
    assert _abort(mod("driver").resolve_questions, "254,259") == "QUESTION_AMBIGUOUS"


@pytest.mark.parametrize("spec", ["999", "c6", "loki", "100", "100a_x"])
def test_resolve_unknown(spec: str) -> None:
    assert _abort(mod("driver").resolve_questions, spec) == "QUESTION_UNKNOWN"


# -- per-question backends ----------------------------------------------------------


@pytest.mark.parametrize(
    ("qid", "backend", "address", "namespace", "host_ports", "needs_k8s", "off"),
    [
        (Q100A, "loki", "127.0.0.1:3110", "app-100a", {3110}, True, False),
        (Q101, "loki", "127.0.0.1:3101", "app-101", {3101}, True, False),
        (Q102, "loki", "127.0.0.1:3102", "app-102", {3102}, True, False),
        (Q282, "loki", "127.0.0.1:10282", "app-282", {10282}, True, False),
        (Q102A, "loki", "127.0.0.1:3100", "app-102a", {3100, 3000}, True, False),
        (Q102B, "loki", "127.0.0.1:3100", "app-102b", {3100, 3000}, True, False),
        (Q259L, "loki", "127.0.0.1:3259", None, {3259}, False, True),
        (Q257, "victorialogs", None, None, set(), False, True),
        (Q258, "victorialogs", None, None, set(), False, True),
    ],
)
def test_log_question_specs(
    qid: str,
    backend: str,
    address: str | None,
    namespace: str | None,
    host_ports: set[int],
    needs_k8s: bool,
    off: bool,
) -> None:
    lb = mod("log_backends")
    spec = lb.LOG_QUESTIONS[qid]
    assert isinstance(spec, lb.LogQuestionSpec)
    assert spec.qid == qid
    assert spec.backend == backend
    assert spec.address == address
    assert spec.namespace == namespace
    # ASSUMPTION: host_ports holds at least the ports the interface names;
    # 102a/102b also hold 3000 (the compose Grafana port).
    assert set(spec.host_ports) >= host_ports
    if not host_ports:
        assert tuple(spec.host_ports) == ()
    assert spec.needs_k8s is needs_k8s
    assert spec.upstream_k8s_disabled is off
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.qid = "x"  # type: ignore[misc]


def test_log_questions_cover_exactly_c5() -> None:
    lb, q = mod("log_backends"), mod("questions")
    # PR-D (lead ruling): log backends are exactly C5; ``kubernetes`` is C2.
    logs = {k for k, s in lb.LOG_QUESTIONS.items() if s.backend != "kubernetes"}
    assert logs == set(C5_ALL)
    kube = {k for k, s in lb.LOG_QUESTIONS.items() if s.backend == "kubernetes"}
    assert kube == set(q.C2_QUESTIONS)
    assert set(lb.LOG_QUESTIONS) == set(C5_ALL) | set(q.C2_QUESTIONS)


def test_backend_of() -> None:
    lb, q = mod("log_backends"), mod("questions")
    for qid in q.B1_QUESTIONS:
        assert lb.backend_of(qid) == "elasticsearch"
    for qid in C5_LOKI:
        assert lb.backend_of(qid) == "loki"
    for qid in C5_VL:
        assert lb.backend_of(qid) == "victorialogs"


def test_victorialogs_image_matches_ci_digest() -> None:
    image = mod("log_backends").VICTORIALOGS_IMAGE
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    digests = [
        line.split("victoria-logs@", 1)[1].strip()
        for line in ci.splitlines()
        if "victoriametrics/victoria-logs@sha256:" in line
    ]
    assert digests, "CI log-backends job no longer pins VictoriaLogs"
    assert "victoriametrics/victoria-logs" in image
    assert image.endswith(digests[0])


# -- ports and config ----------------------------------------------------------------


def test_ports_new_defaults_and_per_attempt_unchanged() -> None:
    ports = mod("driver").Ports()
    assert (
        ports.loki_gateway,
        ports.loki_fake,
        ports.victorialogs_gateway,
        ports.victorialogs,
    ) == (29231, 29232, 29233, 29428)
    assert ports.per_attempt() == {
        "pg": 29210,
        "web": 29280,
        "prometheus": 29290,
        "jaeger_query": 29286,
        "k8s": 29243,
    }


def test_log_ports() -> None:
    ports = mod("driver").Ports()
    # ASSUMPTION: a method of Ports. Fake mode: the per-pass fake backend
    # opens both its ports; gateway/docker: the gateway is round-level.
    loki_fake = set(ports.log_ports("loki", "fake").values())
    assert {29231, 29232} <= loki_fake
    assert 29231 not in set(ports.log_ports("loki", "gateway").values())
    vl_docker = set(ports.log_ports("victorialogs", "docker").values())
    assert 29428 in vl_docker and 29233 not in vl_docker
    vl_fake = set(ports.log_ports("victorialogs", "fake").values())
    assert 29233 in vl_fake
    for value in (
        *ports.log_ports("loki", "fake").values(),
        *ports.log_ports("victorialogs", "docker").values(),
    ):
        assert isinstance(value, int)


def test_config_log_modes_and_hidden_credentials(tmp_path: Path) -> None:
    driver = mod("driver")
    names = {f.name for f in dataclasses.fields(driver.Config)}
    assert {
        "loki_mode",
        "victorialogs_mode",
        "product_loki",
        "product_victorialogs",
    } <= names
    cfg = _cfg(tmp_path)
    assert cfg.loki_mode == "fake"
    assert cfg.victorialogs_mode == "fake"
    cfg.product_loki = dict(LOKI_ENV)
    cfg.product_victorialogs = dict(VL_ENV)
    text = repr(cfg)
    assert (
        LOKI_RO_PW not in text and VL_ENV["OPSPILOT_VICTORIALOGS_PASSWORD"] not in text
    )


# -- environments ----------------------------------------------------------------------


@pytest.fixture
def dirty_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in NOISY.items():
        if key not in BASE:
            monkeypatch.setenv(key, value)


def _b1_envs(tmp_path: Path, **kw: Any) -> tuple[Any, Any, Any, Any]:
    driver, intake = mod("driver"), mod("intake")
    cfg = _cfg(tmp_path)
    account = intake.new_account()
    product_es = {"OPSPILOT_ES_URL": cfg.es_url, "OPSPILOT_ES_API_KEY": "ro-3"}
    identities = tmp_path / "ids.json"
    t = driver.tool_env(cfg, **kw)
    w = driver.web_env(cfg, _pg(), identities, account, **kw)
    k = driver.worker_env(
        cfg, _pg(), product_es, _k8s(tmp_path), None, {"DEEPSEEK_API_KEY": "m-4"}, **kw
    )
    return t, w, k, account


def test_b1_default_envs_byte_identical_to_explicit_elasticsearch(
    tmp_path: Path, dirty_environ: None
) -> None:
    driver = mod("driver")
    t0, w0, k0, a0 = _b1_envs(tmp_path)
    cfg = _cfg(tmp_path)
    t1 = driver.tool_env(cfg, backend="elasticsearch")
    assert t1 == t0
    assert t0["OPSPILOT_ES_URL"] == cfg.es_url
    assert not [
        k for k in t0 if k.startswith(("OPSPILOT_LOKI_", "OPSPILOT_VICTORIALOGS_"))
    ]
    w1 = driver.web_env(cfg, _pg(), tmp_path / "ids.json", a0, backend="elasticsearch")
    # OPSPILOT_UI_USERS is salted per call (PR-B UiAccount.users_env), so it
    # is compared by meaning: same user names, both verify the same password.
    users0, users1 = w0.pop("OPSPILOT_UI_USERS"), w1.pop("OPSPILOT_UI_USERS")
    assert w1 == w0
    assert json.dumps(w1, sort_keys=True) == json.dumps(w0, sort_keys=True)
    from opspilot.web.auth import verify_password

    def names(users: str) -> list[str]:
        return sorted(e.partition("=")[0] for e in users.split(","))

    def verifies(users: str) -> bool:
        return any(
            sep and verify_password(a0.password, encoded)
            for _, sep, encoded in (e.partition("=") for e in users.split(","))
        )

    assert names(users0) == names(users1)
    assert verifies(users0) and verifies(users1)
    product_es = {"OPSPILOT_ES_URL": cfg.es_url, "OPSPILOT_ES_API_KEY": "ro-3"}
    k1 = driver.worker_env(
        cfg, _pg(), product_es, _k8s(tmp_path), None, {"DEEPSEEK_API_KEY": "m-4"},
        backend="elasticsearch", log_env=None,
    )  # fmt: skip
    assert k1 == k0
    assert json.dumps(k1, sort_keys=True) == json.dumps(k0, sort_keys=True)
    for env in (w0, k0):
        assert not [k for k in env if "LOKI" in k or "VICTORIALOGS" in k]


@pytest.mark.parametrize(
    ("backend", "log_env", "prefix", "other"),
    [
        ("loki", LOKI_ENV, "OPSPILOT_LOKI_", "OPSPILOT_VICTORIALOGS_"),
        ("victorialogs", VL_ENV, "OPSPILOT_VICTORIALOGS_", "OPSPILOT_LOKI_"),
    ],
)
def test_log_question_envs_only_their_backend(
    tmp_path: Path,
    dirty_environ: None,
    backend: str,
    log_env: dict[str, str],
    prefix: str,
    other: str,
) -> None:
    driver, intake = mod("driver"), mod("intake")
    cfg = _cfg(tmp_path, questions=(Q100A if backend == "loki" else Q257,))
    if backend == "loki":
        cfg.product_loki = dict(log_env)
    else:
        cfg.product_victorialogs = dict(log_env)
    account = intake.new_account()
    url_key = f"{prefix}URL"
    web = driver.web_env(cfg, _pg(), tmp_path / "ids.json", account, backend=backend)
    worker = driver.worker_env(
        cfg, _pg(), {}, _k8s(tmp_path), None, {"DEEPSEEK_API_KEY": "m-4"},
        backend=backend, log_env=log_env,
    )  # fmt: skip
    for env in (web, worker):
        assert not [k for k in env if k.startswith(("OPSPILOT_ES_", "ELASTICSEARCH_"))]
        assert not [k for k in env if k.startswith(other)]
        assert "VICTORIALOGS_URL" not in env
        assert env[url_key] == log_env[url_key]
    # The web only gets the address.
    assert [k for k in web if k.startswith(prefix)] == [url_key]
    for name, value in log_env.items():
        assert worker[name] == value
    assert worker["OPSPILOT_WORKER_CAPACITY"] == "1"
    assert worker["DEEPSEEK_API_KEY"] == "m-4"
    driver.check_env_isolation(web, "", role="web")
    driver.check_env_isolation(worker, "", role="worker")


def _log_setup_env() -> Any:
    # ASSUMPTION: module not fixed by the interface (next to setup_env).
    return find("log_setup_env", "setup", "log_backends", "driver")


def test_log_setup_env_loki(tmp_path: Path) -> None:
    spec = mod("log_backends").LOG_QUESTIONS[Q100A]
    env = _log_setup_env()(spec, victorialogs_url=None, kubeconfig=None, environ=NOISY)
    assert env == _base(NOISY) | {"TZ": "UTC"}
    kube = tmp_path / "admin.kubeconfig"
    with_kube = _log_setup_env()(
        spec, victorialogs_url=None, kubeconfig=kube, environ=NOISY
    )
    assert with_kube == _base(NOISY) | {"TZ": "UTC", "KUBECONFIG": str(kube)}


def test_log_setup_env_victorialogs() -> None:
    spec = mod("log_backends").LOG_QUESTIONS[Q258]
    env = _log_setup_env()(
        spec,
        victorialogs_url="http://127.0.0.1:29428",
        kubeconfig=None,
        environ=NOISY,
    )
    assert env == _base(NOISY) | {
        "TZ": "UTC",
        "VICTORIALOGS_URL": "http://127.0.0.1:29428",
    }
    for key in env:
        assert not key.startswith(("ELASTICSEARCH_", "OPSPILOT_", "DEEPSEEK", "ZAI"))


def test_log_setup_env_defaults_to_process_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ELASTICSEARCH_API_KEY", ENGINEER)
    monkeypatch.setenv("OPSPILOT_LOKI_PASSWORD", LOKI_RO_PW)
    spec = mod("log_backends").LOG_QUESTIONS[Q259L]
    env = _log_setup_env()(spec, victorialogs_url=None, kubeconfig=None)
    assert env["TZ"] == "UTC"
    assert "ELASTICSEARCH_API_KEY" not in env
    assert LOKI_RO_PW not in " ".join(env.values())


def test_setup_env_unchanged() -> None:
    env = mod("setup").setup_env("http://127.0.0.1:29292", ENGINEER, None, NOISY)
    assert env == _base(NOISY) | {
        "ELASTICSEARCH_URL": "http://127.0.0.1:29292",
        "ELASTICSEARCH_API_KEY": ENGINEER,
        "ES_UPDATE_SHARD_LIMIT": "true",
    }


@pytest.mark.parametrize(
    ("role", "extra"),
    [
        ("web", {"OPSPILOT_LOKI_USERNAME": "ro"}),
        ("web", {"OPSPILOT_LOKI_PASSWORD": LOKI_RO_PW}),
        ("web", {"OPSPILOT_LOKI_BEARER_TOKEN": "tok-fake-13"}),
        ("web", {"OPSPILOT_VICTORIALOGS_USERNAME": "ro"}),
        ("web", {"OPSPILOT_VICTORIALOGS_PASSWORD": "pw"}),
        ("web", {"OPSPILOT_VICTORIALOGS_BEARER_TOKEN": "tok-fake-14"}),
        ("web", {"VICTORIALOGS_URL": "http://127.0.0.1:29428"}),
        ("worker", {"VICTORIALOGS_URL": "http://127.0.0.1:29428"}),
    ],
)
def test_check_env_isolation_log_conditions(role: str, extra: dict[str, str]) -> None:
    driver = mod("driver")
    env = {**BASE, "OPSPILOT_BIND": "127.0.0.1:29280", **extra}
    with pytest.raises(driver.RoundAbort) as caught:
        driver.check_env_isolation(env, ENGINEER, role=role)
    assert caught.value.code == "ENV_ISOLATION"
    for value in extra.values():
        assert value not in str(caught.value)


def test_check_env_isolation_allows_worker_log_credentials() -> None:
    env = {**BASE, **LOKI_ENV, **VL_ENV, "DEEPSEEK_API_KEY": "m-4"}
    mod("driver").check_env_isolation(env, ENGINEER, role="worker")
    web = {**BASE, "OPSPILOT_LOKI_URL": LOKI_ENV["OPSPILOT_LOKI_URL"]}
    mod("driver").check_env_isolation(web, ENGINEER, role="web")


# -- the default fake log backend --------------------------------------------------------


def _auth(env: dict[str, str], prefix: str) -> dict[str, str]:
    token = env.get(f"{prefix}BEARER_TOKEN")
    if token:
        return {"Authorization": f"Bearer {token}"}
    user, pw = env[f"{prefix}USERNAME"], env[f"{prefix}PASSWORD"]
    raw = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def _wrong(env: dict[str, str], prefix: str) -> dict[str, str]:
    if env.get(f"{prefix}BEARER_TOKEN"):
        return {"Authorization": "Bearer not-the-token-15"}
    raw = base64.b64encode(f"{env[f'{prefix}USERNAME']}:wrong-16".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def test_fake_loki_backend_two_ports(tmp_path: Path) -> None:
    driver = mod("driver")
    backend_port, gateway_port = free_port(), free_port()
    started, product = driver.Backends().start_fake_log_backend(
        "loki", backend_port, gateway_port, tmp_path / "fake-loki"
    )
    try:
        prefix = "OPSPILOT_LOKI_"
        assert product[f"{prefix}URL"].rstrip("/") == f"http://127.0.0.1:{gateway_port}"
        backend = f"http://127.0.0.1:{backend_port}"
        gateway = product[f"{prefix}URL"].rstrip("/")
        ns = 1_763_560_000_000_000_000
        push = json.dumps(
            {"streams": [{"stream": {"app": "pay"}, "values": [[str(ns), "boom 1"]]}]}
        ).encode()
        ct = {"Content-Type": "application/json"}
        status, _ = http("POST", f"{backend}/loki/api/v1/push", body=push, headers=ct)
        assert 200 <= status < 300  # engineer side: open, writable
        q = (
            "/loki/api/v1/query_range?query=%7Bapp%3D%22pay%22%7D"
            f"&start={ns - 10**9}&end={ns + 10**9}&direction=forward&limit=10"
        )
        status, body = http("GET", backend + q)
        assert status == 200 and b"boom 1" in body
        status, body = http("GET", gateway + q, headers=_auth(product, prefix))
        assert status == 200 and b"boom 1" in body
        assert http("GET", gateway + q)[0] == 401
        assert http("GET", gateway + q, headers=_wrong(product, prefix))[0] == 401
        status, _ = http(
            "POST", f"{gateway}/loki/api/v1/push", body=push,
            headers={**ct, **_auth(product, prefix)},
        )  # fmt: skip
        assert status == 403
        status, _ = http(
            "POST", f"{gateway}/loki/api/v1/delete?query=%7Bapp%3D%22pay%22%7D",
            headers=_auth(product, prefix),
        )  # fmt: skip
        assert status == 403
    finally:
        started.stop()
    assert not _listening(backend_port)
    assert not _listening(gateway_port)


def test_fake_victorialogs_backend_two_ports(tmp_path: Path) -> None:
    driver = mod("driver")
    backend_port, gateway_port = free_port(), free_port()
    started, product = driver.Backends().start_fake_log_backend(
        "victorialogs", backend_port, gateway_port, tmp_path / "fake-vl"
    )
    try:
        prefix = "OPSPILOT_VICTORIALOGS_"
        gateway = product[f"{prefix}URL"].rstrip("/")
        assert gateway == f"http://127.0.0.1:{gateway_port}"
        backend = f"http://127.0.0.1:{backend_port}"
        read = "/select/logsql/query?query=%2A&limit=10"
        assert http("GET", backend + read)[0] == 200
        assert http("GET", gateway + read, headers=_auth(product, prefix))[0] == 200
        assert http("GET", gateway + read)[0] == 401
        assert http("GET", gateway + read, headers=_wrong(product, prefix))[0] == 401
        line = b'{"_msg":"boom","_time":"2026-11-19T10:00:00Z"}\n'
        status, _ = http(
            "POST", f"{gateway}/insert/jsonline", body=line,
            headers={"Content-Type": "application/stream+json", **_auth(product, prefix)},
        )  # fmt: skip
        assert status == 403
    finally:
        started.stop()
    assert not _listening(gateway_port)


def _listening(port: int) -> bool:
    import socket

    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


# -- command line --------------------------------------------------------------------------


def test_cli_accepts_log_flags(tmp_path: Path) -> None:
    env_file = tmp_path / "secrets.env"
    env_file.write_text("DEEPSEEK_API_KEY=model-fake-value-4\n")
    env_file.chmod(0o600)
    env = {
        k: v
        for k, v in os.environ.items()
        if k in {"PATH", "HOME", "TMPDIR", "LANG", "TZ"} or k.startswith("LC_")
    }
    env["PYTHON_DOTENV_DISABLED"] = "1"
    proc = subprocess.run(
        [
            sys.executable, "-m", "scripts.holmes_evals.driver", "round",
            "--round", "Bad Round!", "--upstream", str(tmp_path),
            "--questions", "c5", "--loki", "gateway", "--victorialogs", "docker",
            "--loki-gateway-port", "39231", "--loki-fake-port", "39232",
            "--victorialogs-gateway-port", "39233", "--victorialogs-port", "39428",
            "--env-file", str(env_file), "--trace", "off", "--no-judge",
            "--allow-dirty", "--tmp-root", str(tmp_path / "tmp"),
            "--evidence-root", str(tmp_path / "evidence"),
        ],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert proc.returncode == 2, proc.stderr
    lines = [json.loads(x) for x in proc.stdout.splitlines() if x.startswith("{")]
    assert lines and lines[-1]["status"] == "aborted", proc.stderr
    assert lines[-1]["code"] == "BAD_ROUND"
    assert "model-fake-value-4" not in proc.stdout + proc.stderr
