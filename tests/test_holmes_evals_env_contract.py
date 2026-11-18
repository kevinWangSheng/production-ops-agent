"""#666 PR-B contract: credentials and process environments (H-4, H-5, H-7,
H-10, H-11, H-23) and the setup script runner.

Independent contract tests written from the task record's PR-B public
interface (``setup.py``, ``judge_run.py``, ``driver.py`` env builders,
``intake.UiAccount``) without reading the implementation.

Rules under test:

* setup/cleanup subprocesses get base variables plus ``ELASTICSEARCH_URL``,
  the engineer ``ELASTICSEARCH_API_KEY`` and ``ES_UPDATE_SHARD_LIMIT=true``
  (``KUBECONFIG`` only when given) and nothing else;
* web and worker environments are built from blank: no ``ELASTICSEARCH_*``
  key, no engineer key value anywhere; the web has no product ES credential,
  no model key and no judge key; the worker gets the read-only ES key,
  ``OPSPILOT_WORKER_CAPACITY=1`` and ``OPSPILOT_CLUSTER_NAME`` only when the
  question has ``cluster_name``;
* ``check_env_isolation`` aborts the round with ``ENV_ISOLATION``;
* the judge subprocess gets base variables, ``PYTHON_DOTENV_DISABLED=1`` and
  only the Z.ai variables;
* ``run_script`` hands the whole script to ``/bin/bash -c`` in the question
  directory with stdin closed, logs to the given file, honours the timeout.

Assumptions beyond the interface (also marked ``# ASSUMPTION:``):

* ``Config`` is built with keyword arguments named as in the interface; the
  ``pg`` handle exposes ``web_dsn``/``runtime_dsn``/``owner_dsn`` as the
  ITBench launcher's does.
* ``purge_question_indices`` returns the deleted index names (order free).
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import socket
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.holmes_evals_b_support import (
    ENGINEER,
    Es,
    new_store,
    no_proxy_env,  # noqa: F401 - fixture
    served,
)
from tests.holmes_evals_support import mod, strings_in

pytestmark = pytest.mark.usefixtures("no_proxy_env")

READONLY = "readonly-fake-value-3"
MODEL = "model-fake-value-4"
JUDGE = "judge-fake-value-5"
K8S = "k8s-fake-value-6"
BASE = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/x",
    "TMPDIR": "/tmp/x",
    "LANG": "C.UTF-8",
    "TZ": "UTC",
    "LC_ALL": "C",
}
NOISY = {
    **BASE,
    "ELASTICSEARCH_URL": "http://127.0.0.1:9200",
    "ELASTICSEARCH_API_KEY": ENGINEER,
    "ELASTICSEARCH_USERNAME": "elastic",
    "DEEPSEEK_API_KEY": MODEL,
    "ZAI_API_KEY": JUDGE,
    "GLM_API_KEY": JUDGE,
    "ZAI_BASE_URL": "https://api.z.ai/api/paas/v4",
    "LANGSMITH_API_KEY": "ls-fake-value-7",
    "OPSPILOT_ES_API_KEY": READONLY,
    "OPSPILOT_TRACE": "lab",
    "OPENAI_API_KEY": "oa-fake-value-8",
    "http_proxy": "http://127.0.0.1:7890",
    "PYTHONPATH": "/x",
}


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
        "es_readonly_env": None,
        "k8s_env_file": None,
        "kubeconfig": None,
        "env_file": None,
        "trace": "off",
        "judge": False,
        "allow_dirty": True,
        "tmp_root": tmp_path / "tmp",
        "evidence_root": tmp_path / "evidence",
        **overrides,
    }
    names = {f.name for f in dataclasses.fields(driver.Config)}
    return driver.Config(**{k: v for k, v in wanted.items() if k in names})


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
        token=K8S,
        namespace="otel-demo",
    )


def _ui_user_verifies(users: str, password: str) -> bool:
    from opspilot.web.auth import verify_password

    for entry in users.split(","):
        _, sep, encoded = entry.partition("=")
        if sep and verify_password(password, encoded):
            return True
    return False


def _no_engineer(env: dict[str, str]) -> None:
    assert not [k for k in env if k.startswith("ELASTICSEARCH_")]
    assert not [k for k, v in env.items() if ENGINEER in str(v)]


# -- setup subprocess env -----------------------------------------------------------


def test_setup_env_is_base_plus_engineer_es_only(tmp_path: Path) -> None:
    setup = mod("setup")
    env = setup.setup_env("http://127.0.0.1:29292", ENGINEER, None, NOISY)
    assert env == _base(NOISY) | {
        "ELASTICSEARCH_URL": "http://127.0.0.1:29292",
        "ELASTICSEARCH_API_KEY": ENGINEER,
        "ES_UPDATE_SHARD_LIMIT": "true",
    }
    kube = tmp_path / "admin.kubeconfig"
    with_kube = setup.setup_env("http://127.0.0.1:29292", ENGINEER, kube, NOISY)
    assert with_kube == env | {"KUBECONFIG": str(kube)}
    assert not [k for k in with_kube if k.startswith("OPSPILOT_")]
    assert setup.DEFAULT_SETUP_TIMEOUT == 300


def test_judge_env_is_narrowed() -> None:
    judge_run = mod("judge_run")
    assert judge_run.judge_env(NOISY) == _base(NOISY) | {
        "PYTHON_DOTENV_DISABLED": "1",
        "ZAI_API_KEY": JUDGE,
        "GLM_API_KEY": JUDGE,
        "ZAI_BASE_URL": "https://api.z.ai/api/paas/v4",
    }
    only = {**BASE, "ZAI_API_KEY": JUDGE, "ELASTICSEARCH_API_KEY": ENGINEER}
    assert judge_run.judge_env(only) == _base(only) | {
        "PYTHON_DOTENV_DISABLED": "1",
        "ZAI_API_KEY": JUDGE,
    }


# -- run_script --------------------------------------------------------------------


def _run(tmp_path: Path, script: str | None, *, env=None, timeout_s: int = 30):
    qdir = tmp_path / "question"
    qdir.mkdir(exist_ok=True)
    log = tmp_path / "logs" / "before.log"
    log.parent.mkdir(exist_ok=True)
    result = mod("setup").run_script(
        script,
        operation="before_test",
        cwd=qdir,
        env=env if env is not None else dict(BASE, PATH=os.environ["PATH"]),
        timeout_s=timeout_s,
        log_path=log,
    )
    return result, qdir, log


def test_run_script_bash_cwd_log_and_exit_code(tmp_path: Path) -> None:
    script = (
        'echo "out-line"\n'
        'echo "err-line" >&2\n'
        "pwd\n"
        '[[ "a" == "a" ]] && echo "bash-ok"\n'
        "exit 3\n"
    )
    result, qdir, log = _run(tmp_path, script)
    assert result.operation == "before_test"
    assert result.exit_code == 3
    assert result.timed_out is False
    assert result.skipped is False
    text = log.read_text()
    assert "out-line" in text and "err-line" in text and "bash-ok" in text
    assert str(qdir.resolve()) in text or str(qdir) in text
    assert result.log_sha256 == hashlib.sha256(log.read_bytes()).hexdigest()
    assert result.seconds >= 0
    assert result.started_at <= result.ended_at


def test_run_script_stdin_closed_and_env_not_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEAK_PROBE_VAR", "leaked")
    env = dict(BASE, PATH=os.environ["PATH"], ELASTICSEARCH_URL="http://x")
    script = (
        'if read -r line; then echo "stdin=got"; else echo "stdin=eof"; fi\n'
        'echo "probe=${LEAK_PROBE_VAR:-unset}"\n'
        'echo "es=${ELASTICSEARCH_URL}"\n'
    )
    # Our own fd 0 becomes a pipe holding a line: an inherited stdin would
    # read it; a closed one (or /dev/null) reads EOF.
    read_end, write_end = os.pipe()
    os.write(write_end, b"from-parent\n")
    saved = os.dup(0)
    os.dup2(read_end, 0)
    started = time.monotonic()
    try:
        result, _, log = _run(tmp_path, script, env=env, timeout_s=20)
    finally:
        os.dup2(saved, 0)
        for fd in (saved, read_end, write_end):
            os.close(fd)
    assert time.monotonic() - started < 15
    assert result.exit_code == 0
    text = log.read_text()
    assert "stdin=eof" in text
    assert "probe=unset" in text
    assert "es=http://x" in text


def test_run_script_timeout(tmp_path: Path) -> None:
    started = time.monotonic()
    result, _, _ = _run(tmp_path, "sleep 30\n", timeout_s=1)
    assert time.monotonic() - started < 15
    assert result.timed_out is True
    assert result.exit_code != 0


@pytest.mark.parametrize("script", [None, ""])
def test_run_script_empty_is_skipped(tmp_path: Path, script: str | None) -> None:
    result, _, _ = _run(tmp_path, script)
    assert result.skipped is True
    assert result.timed_out is False


def test_script_result_never_carries_env_values(tmp_path: Path) -> None:
    env = dict(BASE, PATH=os.environ["PATH"], ELASTICSEARCH_API_KEY=ENGINEER)
    result, _, _ = _run(tmp_path, 'echo "$ELASTICSEARCH_API_KEY"\n', env=env)
    assert result.exit_code == 0
    assert not [s for s in strings_in(result) if ENGINEER in s]
    assert ENGINEER not in repr(result)


# -- purge -------------------------------------------------------------------------


def test_purge_question_indices_only_this_question() -> None:
    store = new_store()
    es = Es(store)
    for name in ("app-275-logs-a", "app-275-old", "app-2750-x", "app-276-logs"):
        es.create(name, {"m": {"type": "keyword"}})
    with served(store) as url:
        admin = mod("shift").EsAdmin(url, ENGINEER)
        purged = mod("setup").purge_question_indices(
            admin, "275_es_field_name_correction"
        )
    assert sorted(purged) == ["app-275-logs-a", "app-275-old"]
    rows = es.ok("GET", "/_cat/indices", query={"format": "json", "h": "index"})
    assert sorted(r["index"] for r in rows) == ["app-2750-x", "app-276-logs"]


def test_purge_many_184_style_names_by_name_under_stock_settings() -> None:
    """The fake ES refuses wildcard DELETE by default (stock ES 8,
    destructive_requires_name); purge deletes by explicit name, so it still
    removes every index of the question and leaves ``app-1840-*`` alone."""
    store = new_store()
    es = Es(store)
    names = [
        f"app-184-{team}-{service}-2024.01.{day:02d}"
        for team in ("platform", "payments", "search")
        for service in ("billing", "api", "worker")
        for day in range(1, 31)
    ]
    for name in names:
        es.create(name, {"@timestamp": {"type": "date"}})
    for name in ("app-1840-other", "app-1840-x-2024.01.01", "app-185-geo-a"):
        es.create(name, {"m": {"type": "keyword"}})
    with served(store) as url:
        admin = mod("shift").EsAdmin(url, ENGINEER)
        purged = mod("setup").purge_question_indices(
            admin, "184_elasticsearch_index_explosion"
        )
    assert sorted(purged) == sorted(names)
    rows = es.ok("GET", "/_cat/indices", query={"format": "json", "h": "index"})
    assert sorted(r["index"] for r in rows) == [
        "app-1840-other",
        "app-1840-x-2024.01.01",
        "app-185-geo-a",
    ]


# -- web / worker env ---------------------------------------------------------------


@pytest.fixture
def dirty_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    """Builders must not copy from the parent environment."""
    for key, value in NOISY.items():
        if key not in BASE:
            monkeypatch.setenv(key, value)


def test_ports_defaults() -> None:
    ports = mod("driver").Ports()
    assert (
        ports.pg,
        ports.web,
        ports.es,
        ports.prometheus,
        ports.jaeger_query,
        ports.k8s,
    ) == (29210, 29280, 29292, 29290, 29286, 29243)


def test_config_fields_and_evidence_dir(tmp_path: Path) -> None:
    driver = mod("driver")
    names = {f.name for f in dataclasses.fields(driver.Config)}
    assert {
        "round", "upstream", "questions", "ports", "es_mode", "es_url",
        "es_readonly_env", "k8s_env_file", "kubeconfig", "env_file", "trace",
        "judge", "allow_dirty", "tmp_root", "evidence_root", "poll_seconds",
        "terminal_wait",
    } <= names  # fmt: skip
    cfg = _cfg(tmp_path, round="r7")
    assert Path(cfg.evidence_dir) == tmp_path / "evidence" / "holmes-evals-r7"


def test_web_env_has_no_es_model_or_judge_credentials(
    tmp_path: Path, dirty_environ: None
) -> None:
    driver, intake = mod("driver"), mod("intake")
    account = intake.new_account()
    identities = tmp_path / "identities.json"
    env = driver.web_env(_cfg(tmp_path), _pg(), identities, account)
    _no_engineer(env)
    for name in (
        "OPSPILOT_ES_API_KEY",
        "OPSPILOT_ES_USERNAME",
        "OPSPILOT_ES_PASSWORD",
        "DEEPSEEK_API_KEY",
        "ZAI_API_KEY",
        "GLM_API_KEY",
        "OPENAI_API_KEY",
        "LANGSMITH_API_KEY",
    ):
        assert name not in env, name
    values = " ".join(env.values())
    for secret in (READONLY, MODEL, JUDGE, K8S):
        assert secret not in values
    assert account.password not in values
    assert "http_proxy" not in env and "PYTHONPATH" not in env
    assert account.password not in env["OPSPILOT_UI_USERS"]
    assert _ui_user_verifies(env["OPSPILOT_UI_USERS"], account.password)
    assert env["OPSPILOT_TARGET_IDENTITIES"] == str(identities)
    driver.check_env_isolation(env, ENGINEER, role="web")


@pytest.mark.parametrize("cluster", [None, "production-eu-west-2"])
def test_worker_env_readonly_key_capacity_and_cluster_name(
    tmp_path: Path, dirty_environ: None, cluster: str | None
) -> None:
    driver = mod("driver")
    product_es = {
        "OPSPILOT_ES_URL": "http://127.0.0.1:29292",
        "OPSPILOT_ES_API_KEY": READONLY,
    }
    env = driver.worker_env(
        _cfg(tmp_path),
        _pg(),
        product_es,
        _k8s(tmp_path),
        cluster,
        {"DEEPSEEK_API_KEY": MODEL},
    )
    _no_engineer(env)
    assert env["OPSPILOT_ES_API_KEY"] == READONLY
    assert env["OPSPILOT_ES_URL"] == "http://127.0.0.1:29292"
    assert env["OPSPILOT_WORKER_CAPACITY"] == "1"
    assert env["DEEPSEEK_API_KEY"] == MODEL
    for name in ("ZAI_API_KEY", "GLM_API_KEY", "OPENAI_API_KEY"):
        assert name not in env, name
    assert JUDGE not in " ".join(env.values())
    if cluster is None:
        assert "OPSPILOT_CLUSTER_NAME" not in env
    else:
        assert env["OPSPILOT_CLUSTER_NAME"] == cluster
    driver.check_env_isolation(env, ENGINEER, role="worker")


def _abort_code(fn, *args, **kwargs) -> str | None:
    driver = mod("driver")
    with pytest.raises(driver.RoundAbort) as caught:
        fn(*args, **kwargs)
    assert ENGINEER not in str(caught.value)
    assert ENGINEER not in str(getattr(caught.value, "detail", ""))
    return caught.value.code


@pytest.mark.parametrize(
    ("role", "extra"),
    [
        ("web", {"ELASTICSEARCH_URL": "http://127.0.0.1:29292"}),
        ("worker", {"ELASTICSEARCH_API_KEY": "anything"}),
        ("worker", {"ELASTICSEARCH_HOSTS": "http://127.0.0.1:29292"}),
        ("worker", {"OPSPILOT_ES_API_KEY": ENGINEER}),
        ("worker", {"SOME_DSN": f"postgresql://u:{ENGINEER}@127.0.0.1/db"}),
        ("web", {"X": f"prefix-{ENGINEER}-suffix"}),
        ("web", {"OPSPILOT_ES_API_KEY": READONLY}),
        ("web", {"OPSPILOT_ES_USERNAME": "ro"}),
        ("web", {"OPSPILOT_ES_PASSWORD": "pw"}),
        ("web", {"DEEPSEEK_API_KEY": MODEL}),
        ("web", {"ZAI_API_KEY": JUDGE}),
    ],
)
def test_check_env_isolation_aborts(role: str, extra: dict[str, str]) -> None:
    env = {**BASE, "OPSPILOT_BIND": "127.0.0.1:29280", **extra}
    code = _abort_code(mod("driver").check_env_isolation, env, ENGINEER, role=role)
    assert code == "ENV_ISOLATION"


def test_check_env_isolation_allows_worker_product_credentials() -> None:
    env = {
        **BASE,
        "OPSPILOT_ES_URL": "http://127.0.0.1:29292",
        "OPSPILOT_ES_API_KEY": READONLY,
        "DEEPSEEK_API_KEY": MODEL,
        "OPSPILOT_K8S_TOKEN": K8S,
    }
    mod("driver").check_env_isolation(env, ENGINEER, role="worker")


def test_k8s_target_token_not_in_repr(tmp_path: Path) -> None:
    target = _k8s(tmp_path)
    assert K8S not in repr(target)


def test_unreachable_k8s_target(tmp_path: Path) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    target = mod("driver").unreachable_k8s(tmp_path, port)
    assert target.api_url.startswith("https://127.0.0.1:")
    assert target.api_url.rstrip("/").endswith(f":{port}")
    assert Path(target.ca_file).is_file()
    assert "BEGIN CERTIFICATE" in Path(target.ca_file).read_text()
    assert target.token and target.token not in repr(target)
    with socket.socket() as probe:
        probe.settimeout(1)
        assert probe.connect_ex(("127.0.0.1", port)) != 0
