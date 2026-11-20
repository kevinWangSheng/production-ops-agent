"""#666 PR-D contract: C2 question lists, per-question specs, ports, config,
``run_mode`` and the process environments (no running stack).

Independent contract tests written from the task record's "PR-D 接口"
section (docs/tasks/2026-11-18-holmes-evals-666.md) without reading the
PR-D implementation. No PG, web, worker, kind, model or network.

Rules under test:

* ``C2_QUESTIONS`` (six ids), ``ALL_QUESTIONS = B1 + C5 + C2``, group
  ``c2``; ``resolve_questions`` accepts ``c2`` and the six numbers;
* R8-13 by metadata: the six are not in ``R8_13_SKILL_ASSERTION_DROPPED``
  (pinned; against the real checkout their ``memories_generated`` is empty);
* ``LOG_QUESTIONS`` entries with backend ``kubernetes``: 89/90 without
  namespace and k8s, 94/95/96/162 with ``app-<n>`` and k8s; ``backend_of``;
  ``KNOWLEDGE_QUESTIONS``;
* ``Ports.log_ports("kubernetes", ...)`` empty, ``Config.log_mode`` ``none``;
* ``tool_env``/``web_env``/``worker_env``: ``kubernetes`` has no log
  backend; ``knowledge=True`` sets ``OPSPILOT_KNOWLEDGE_TOOL=1`` on both web
  and worker; ``import_account`` is appended to ``OPSPILOT_UI_USERS``; B1
  and C5 defaults byte-identical with the new keywords at their defaults;
* ``log_setup_env`` for ``kubernetes``: only base + ``KUBECONFIG``;
* ``run_mode(root, qid)`` reads ``test_type`` only; ``Question`` and
  ``WHITELIST`` unchanged.

Assumptions beyond the interface (also marked ``# ASSUMPTION:``):

* ``KNOWLEDGE_QUESTIONS`` lives in ``log_backends`` (or ``driver``/
  ``questions``); ``run_mode`` in ``questions``.
* ``OPSPILOT_UI_USERS`` keeps the question account first and appends the
  import account after one ``,``.
* ``log_setup_env`` for ``kubernetes`` does not set ``TZ`` (the interface:
  "只加 KUBECONFIG"); the base variables come from the host as for C5.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from tests.holmes_evals_c_support import C5_ALL, Q100A, Q257, find
from tests.holmes_evals_d_support import (
    C2_ALL,
    C2_K8S,
    C2_NO_K8S,
    IMPORT_PREFIX,
    NAMESPACE,
    Q89,
    Q90,
    Q94,
    Q95,
    Q96,
    Q162,
)
from tests.holmes_evals_support import FIXTURES, mod, upstream_root, write_question

Q275 = "275_es_field_name_correction"
PR_A_WHITELIST = (
    "user_prompt", "tags", "toolsets", "before_test", "after_test",
    "setup_timeout", "port_forwards", "include_tool_calls",
    "memories_generated", "skip", "enable_todo", "evaluation", "cluster_name",
)  # fmt: skip


def _cfg(tmp_path: Path, **overrides: Any) -> Any:
    driver = mod("driver")
    wanted: dict[str, Any] = {
        "round": "t1",
        "upstream": tmp_path / "upstream",
        "questions": (Q275,),
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
    ca.write_text("fake ca\n")
    return mod("driver").K8sTarget(
        api_url="https://127.0.0.1:29243", ca_file=str(ca), token="k8s-fake-value-6"
    )


def _account(name: str, password: str) -> Any:
    return mod("intake").UiAccount(name, password)


@pytest.fixture
def fixed_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    """``users_env`` salts every call; pin it so two envs compare."""
    intake = mod("intake")
    monkeypatch.setattr(intake, "hash_password", lambda pw: f"pbkdf2-fixed-{len(pw)}")


SECRETS = {"DEEPSEEK_API_KEY": "model-fake-value-4"}


# -- question lists ----------------------------------------------------------------------------


def test_c2_questions_and_groups() -> None:
    q = mod("questions")
    assert tuple(q.C2_QUESTIONS) == C2_ALL
    assert tuple(q.ALL_QUESTIONS) == (
        tuple(q.B1_QUESTIONS) + tuple(q.C5_QUESTIONS) + C2_ALL
    )
    assert tuple(q.QUESTION_GROUPS["c2"]) == C2_ALL
    assert set(q.QUESTION_GROUPS) >= {"b1", "c5", "c2"}
    assert len(set(q.ALL_QUESTIONS)) == len(q.ALL_QUESTIONS)
    # B1 / C5 unchanged.
    assert len(q.B1_QUESTIONS) == 30 and tuple(q.C5_QUESTIONS) == C5_ALL
    assert tuple(q.QUESTION_GROUPS["c5"]) == C5_ALL


def test_resolve_questions_c2() -> None:
    driver = mod("driver")
    assert driver.resolve_questions("c2") == C2_ALL
    assert driver.resolve_questions("89,162") == (Q89, Q162)
    assert driver.resolve_questions("94_skill_transparency") == (Q94,)
    for n, qid in (("90", Q90), ("95", Q95), ("96", Q96)):
        assert driver.resolve_questions(n) == (qid,)
    assert driver.resolve_questions("") == tuple(mod("questions").B1_QUESTIONS)
    both = driver.resolve_questions("c5,c2")
    assert both == C5_ALL + C2_ALL


def test_c2_not_in_r8_13_set() -> None:
    q = mod("questions")
    for qid in C2_ALL:
        assert qid not in q.R8_13_SKILL_ASSERTION_DROPPED, qid
        assert qid not in q.SKILL_ASSERTION_DROPPED, qid
    # The existing set is unchanged.
    assert q.R8_13_SKILL_ASSERTION_DROPPED == q.SKILL_ASSERTION_DROPPED | {
        "282_loki_custom_stream_label"
    }


def test_c2_memories_generated_empty_upstream_pins_r8_13() -> None:
    """If upstream metadata ever gains ``memories_generated`` for these six,
    this test fails and R8-13 has to be revisited (interface)."""
    root = upstream_root()
    if root is None:
        pytest.skip("pinned upstream checkout not on this machine")
    q = mod("questions")
    for qid in C2_ALL:
        loaded = q.load_question(root, qid)
        assert not loaded.memories_generated, qid
        assert qid not in q.R8_13_SKILL_ASSERTION_DROPPED


def test_question_whitelist_unchanged() -> None:
    q = mod("questions")
    assert tuple(q.WHITELIST) == PR_A_WHITELIST
    assert "test_type" not in q.WHITELIST
    assert "test_type" not in q.Question.__dataclass_fields__


# -- run_mode -------------------------------------------------------------------------------


def test_run_mode_reads_test_type_only(tmp_path: Path) -> None:
    q = mod("questions")
    run_mode = find("run_mode", "questions")
    write_question(
        tmp_path,
        Q162,
        {"user_prompt": "p", "test_type": "server", "expected_output": ["ANS-1"]},
    )
    write_question(tmp_path, Q94, {"user_prompt": "p", "expected_output": ["ANS-2"]})
    assert run_mode(tmp_path, Q162) == "server"
    assert run_mode(tmp_path, Q94) is None
    # The loader still keeps only the PR-A whitelist.
    assert not hasattr(q.load_question(tmp_path, Q162), "test_type")


def test_run_mode_on_pinned_checkout() -> None:
    root = upstream_root()
    if root is None:
        pytest.skip("pinned upstream checkout not on this machine")
    run_mode = find("run_mode", "questions")
    # Read with yaml here only for the one allowed key.
    for qid in C2_ALL:
        doc = yaml.safe_load((root / FIXTURES / qid / "test_case.yaml").read_text())
        assert run_mode(root, qid) == doc.get("test_type"), qid


# -- per-question specs ----------------------------------------------------------------------


def test_log_questions_kubernetes_specs() -> None:
    lb = mod("log_backends")
    for qid in C2_ALL:
        spec = lb.LOG_QUESTIONS[qid]
        assert spec.qid == qid
        assert spec.backend == "kubernetes"
        assert spec.address is None
        assert tuple(spec.host_ports) == ()
        assert spec.upstream_k8s_disabled is False
        assert lb.backend_of(qid) == "kubernetes"
    for qid in C2_NO_K8S:
        assert lb.LOG_QUESTIONS[qid].namespace is None
        assert lb.LOG_QUESTIONS[qid].needs_k8s is False
    for qid in C2_K8S:
        assert lb.LOG_QUESTIONS[qid].namespace == NAMESPACE[qid]
        assert lb.LOG_QUESTIONS[qid].needs_k8s is True
    # Existing entries unchanged.
    assert lb.backend_of(Q275) == "elasticsearch"
    assert lb.backend_of(Q100A) == "loki"
    assert lb.backend_of(Q257) == "victorialogs"
    assert set(lb.LOG_QUESTIONS) == set(C5_ALL) | set(C2_ALL)


def test_knowledge_questions() -> None:
    kq = find("KNOWLEDGE_QUESTIONS", "log_backends", "driver", "questions")
    assert isinstance(kq, frozenset)
    assert kq == frozenset(C2_ALL)


def test_ports_and_log_mode_for_kubernetes(tmp_path: Path) -> None:
    driver = mod("driver")
    ports = driver.Ports()
    for mode in ("fake", "gateway", "docker", "none", "external"):
        assert ports.log_ports("kubernetes", mode) == {}
    cfg = _cfg(tmp_path)
    assert cfg.log_mode("kubernetes") == "none"
    assert cfg.log_mode("elasticsearch") == "fake"
    assert cfg.log_mode("loki") == "fake"


# -- environments -----------------------------------------------------------------------------


def _log_keys(env: dict[str, str]) -> list[str]:
    return [
        k
        for k in env
        if k.startswith(("OPSPILOT_ES_", "OPSPILOT_LOKI_", "OPSPILOT_VICTORIALOGS_"))
    ]


def test_tool_env_kubernetes_has_no_log_backend(tmp_path: Path) -> None:
    driver = mod("driver")
    cfg = _cfg(tmp_path)
    env = driver.tool_env(cfg, "kubernetes")
    assert _log_keys(env) == []
    es = driver.tool_env(cfg, "elasticsearch")
    assert {k: v for k, v in es.items() if k != "OPSPILOT_ES_URL"} == env
    assert "OPSPILOT_KNOWLEDGE_TOOL" not in env
    assert driver.tool_env(cfg, "kubernetes", knowledge=True) == {
        **env,
        "OPSPILOT_KNOWLEDGE_TOOL": "1",
    }


@pytest.mark.usefixtures("fixed_hash")
def test_defaults_byte_identical_for_b1_and_c5(tmp_path: Path) -> None:
    driver = mod("driver")
    cfg = _cfg(tmp_path)
    pg, k8s = _pg(), _k8s(tmp_path)
    acct = _account("holmes-eval-aaaa", "pw-fake-1")
    ident = tmp_path / "ids.json"
    product_es = {"OPSPILOT_ES_URL": cfg.es_url, "OPSPILOT_ES_API_KEY": "ro-fake-3"}
    for backend in ("elasticsearch", "loki", "victorialogs"):
        assert driver.tool_env(cfg, backend) == driver.tool_env(
            cfg, backend, knowledge=False
        )
        web = driver.web_env(cfg, pg, ident, acct, backend=backend)
        assert web == driver.web_env(
            cfg, pg, ident, acct, backend=backend, knowledge=False, import_account=None
        )
        assert "OPSPILOT_KNOWLEDGE_TOOL" not in web
        assert web["OPSPILOT_UI_USERS"] == acct.users_env()
        worker = driver.worker_env(
            cfg, pg, product_es, k8s, None, SECRETS, backend=backend
        )
        assert worker == driver.worker_env(
            cfg, pg, product_es, k8s, None, SECRETS, backend=backend, knowledge=False
        )
        assert "OPSPILOT_KNOWLEDGE_TOOL" not in worker


@pytest.mark.usefixtures("fixed_hash")
def test_knowledge_on_both_web_and_worker(tmp_path: Path) -> None:
    driver = mod("driver")
    cfg = _cfg(tmp_path, questions=C2_ALL)
    pg, k8s = _pg(), _k8s(tmp_path)
    acct = _account("holmes-eval-bbbb", "pw-fake-2")
    imp = _account(IMPORT_PREFIX + "cccc", "pw-fake-3")
    ident = tmp_path / "ids.json"
    web = driver.web_env(
        cfg, pg, ident, acct, backend="kubernetes", knowledge=True, import_account=imp
    )
    worker = driver.worker_env(
        cfg, pg, {}, k8s, None, SECRETS, backend="kubernetes", knowledge=True
    )
    assert web["OPSPILOT_KNOWLEDGE_TOOL"] == "1"
    assert worker["OPSPILOT_KNOWLEDGE_TOOL"] == "1"
    for env in (web, worker):
        assert _log_keys(env) == []
    # The worker keeps the product's K8s read-only identity.
    assert worker["OPSPILOT_K8S_API_URL"] == k8s.api_url
    assert worker["OPSPILOT_K8S_TOKEN"] == k8s.token
    assert worker["OPSPILOT_WORKER_DSN"] == pg.runtime_dsn
    # Same tool face on both sides (a mismatch is refused at claim time).
    shared = driver.tool_env(cfg, "kubernetes", knowledge=True)
    for key, value in shared.items():
        assert web[key] == value and worker[key] == value
    # Without the import account, the web env differs only by the switch.
    plain = driver.web_env(cfg, pg, ident, acct, backend="kubernetes")
    assert {k: v for k, v in web.items() if k not in ("OPSPILOT_KNOWLEDGE_TOOL",
            "OPSPILOT_UI_USERS")} == {k: v for k, v in plain.items()
            if k != "OPSPILOT_UI_USERS"}  # fmt: skip


@pytest.mark.usefixtures("fixed_hash")
def test_import_account_appended_to_ui_users(tmp_path: Path) -> None:
    driver = mod("driver")
    cfg = _cfg(tmp_path, questions=C2_ALL)
    acct = _account("holmes-eval-dddd", "pw-fake-4")
    imp = _account(IMPORT_PREFIX + "eeee", "pw-fake-5")
    web = driver.web_env(
        cfg, _pg(), tmp_path / "i.json", acct, backend="kubernetes",
        knowledge=True, import_account=imp,
    )  # fmt: skip
    # ASSUMPTION: question account first, import account appended.
    assert web["OPSPILOT_UI_USERS"] == f"{acct.users_env()},{imp.users_env()}"
    assert "pw-fake-4" not in web["OPSPILOT_UI_USERS"]
    assert "pw-fake-5" not in web["OPSPILOT_UI_USERS"]


def test_log_setup_env_kubernetes(tmp_path: Path) -> None:
    log_setup_env = find("log_setup_env", "setup", "log_backends", "driver")
    lb = mod("log_backends")
    noisy = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/x",
        "LANG": "C.UTF-8",
        "ELASTICSEARCH_API_KEY": "engineer-fake-value-1",
        "DEEPSEEK_API_KEY": "model-fake-value-4",
        "VICTORIALOGS_URL": "http://elsewhere:9428",
        "OPSPILOT_ES_API_KEY": "ro-fake-3",
    }
    kube = tmp_path / "kubeconfig"
    for qid in C2_ALL:
        spec = lb.LOG_QUESTIONS[qid]
        with_kube = log_setup_env(
            spec, victorialogs_url="http://127.0.0.1:29428", kubeconfig=kube,
            environ=noisy,
        )  # fmt: skip
        without = log_setup_env(
            spec, victorialogs_url="http://127.0.0.1:29428", kubeconfig=None,
            environ=noisy,
        )  # fmt: skip
        assert with_kube == {**without, "KUBECONFIG": str(kube)}
        assert "KUBECONFIG" not in without
        for env in (with_kube, without):
            assert "VICTORIALOGS_URL" not in env
            assert "TZ" not in env
            assert not [
                k for k in env
                if k.startswith(("OPSPILOT_", "ELASTICSEARCH_", "DEEPSEEK", "ZAI"))
            ]  # fmt: skip
            assert env["PATH"] == noisy["PATH"]


def test_check_env_isolation_unchanged_for_kubernetes(tmp_path: Path) -> None:
    """No new credential: a C2 worker env passes the PR-B/C check as is."""
    driver = mod("driver")
    cfg = _cfg(tmp_path, questions=C2_ALL)
    pg, k8s = _pg(), _k8s(tmp_path)
    worker = driver.worker_env(
        cfg, pg, {}, k8s, None, SECRETS, backend="kubernetes", knowledge=True
    )
    web = driver.web_env(
        cfg, pg, tmp_path / "i.json", _account("holmes-eval-ffff", "pw-fake-6"),
        backend="kubernetes", knowledge=True,
        import_account=_account(IMPORT_PREFIX + "gggg", "pw-fake-7"),
    )  # fmt: skip
    driver.check_env_isolation(worker, "engineer-fake-value-1", role="worker")
    driver.check_env_isolation(web, "engineer-fake-value-1", role="web")
