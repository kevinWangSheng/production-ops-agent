"""#666 PR-E contract: question lists and groups, the 283 spec and the
prefix release check, the TodoWrite switch (``todo_for``, ``Config``, the
command line, the process environments), ``task_list_stats_from_rows`` and
the TodoWrite shape of the ``include_tool_calls`` attachment (no stack).

Independent contract tests written from the task record's "PR-E 接口"
section and the C1 record sections 1, 2, 6 and 10, without reading the PR-E
implementation. No PG, web, worker, kind, model or network; ``kubectl`` is a
shell stand-in on ``PATH``.

Assumptions beyond the interface (also marked ``# ASSUMPTION:``):

* ``todo_for`` lives in ``driver`` (or ``questions``);
  ``namespaces_with_prefix`` and ``task_list_stats_from_rows`` in
  ``log_backends``/``driver``/``tool_calls`` (the interface names the
  ``Backends`` field and the pure function, not the module).
* ``namespaces_with_prefix`` returns bare names; a ``namespace/`` prefix from
  ``kubectl -o name`` is accepted as well. ``kubectl`` is found through the
  ``PATH`` of the ``env`` it is given (both that and the process ``PATH``
  point at the stand-in here).
* ``task_list_stats_from_rows`` orders by ``(sequence, step_id)`` then
  ``ordinal`` as ``tool_calls_from_rows`` does (PR-B); ``last`` counts come
  from the last ok list's ``todos``/``counts`` (the product writes both).
* A TodoWrite call in the attachment: ``params`` is ``{"todos": [...]}``
  without any secret the model wrote (redacted), ``output`` carries the
  stored result's ``todos`` and ``status``.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from tests.holmes_evals_c_support import C5_ALL, find
from tests.holmes_evals_d_support import C2_ALL
from tests.holmes_evals_e_support import (
    C1_ALL,
    HARD,
    NS_283,
    PREFIX,
    Q184,
    Q275,
    Q283,
    TODO_ENV,
    TODO_SENTINEL,
    query_ok,
    query_refused,
    step,
    todo_result,
    todos,
    write_283,
)
from tests.holmes_evals_support import mod, write_question
from tests.test_holmes_evals_d_contract import (
    SECRETS,
    _account,
    _cfg,
    _k8s,
    _pg,
    fixed_hash,  # noqa: F401 - fixture
)

STEP_A = "aaaaaaaa-0000-4000-8000-000000000001"
STEP_B = "bbbbbbbb-0000-4000-8000-000000000002"
STEP_C = "cccccccc-0000-4000-8000-000000000003"
STEP_L = "dddddddd-0000-4000-8000-000000000004"


# -- question lists ------------------------------------------------------------------------


def test_c1_lists_and_groups() -> None:
    q = mod("questions")
    assert tuple(q.C1_QUESTIONS) == C1_ALL
    assert tuple(q.TODO_HARD_QUESTIONS) == HARD
    assert set(HARD) <= set(q.B1_QUESTIONS)
    assert tuple(q.ALL_QUESTIONS) == (tuple(q.B1_QUESTIONS) + C5_ALL + C2_ALL + C1_ALL)
    assert len(set(q.ALL_QUESTIONS)) == len(q.ALL_QUESTIONS)
    assert tuple(q.QUESTION_GROUPS["c1"]) == C1_ALL
    assert tuple(q.QUESTION_GROUPS["todo-hard"]) == HARD
    # Existing groups unchanged.
    assert tuple(q.QUESTION_GROUPS["c5"]) == C5_ALL
    assert tuple(q.QUESTION_GROUPS["c2"]) == C2_ALL
    assert tuple(q.QUESTION_GROUPS["b1"]) == tuple(q.B1_QUESTIONS)
    assert len(q.B1_QUESTIONS) == 30
    # 283 names one question only.
    assert [x for x in q.ALL_QUESTIONS if x.split("_", 1)[0] == "283"] == [Q283]


def test_resolve_c1_and_todo_hard() -> None:
    driver = mod("driver")
    assert driver.resolve_questions("283") == (Q283,)
    assert driver.resolve_questions(Q283) == (Q283,)
    assert driver.resolve_questions("c1") == C1_ALL
    assert driver.resolve_questions("todo-hard") == HARD
    assert driver.resolve_questions("184") == (Q184,)
    assert driver.resolve_questions("") == tuple(mod("questions").B1_QUESTIONS)
    assert driver.resolve_questions("c2,c1") == C2_ALL + C1_ALL


def test_283_not_in_r8_13_or_knowledge_sets() -> None:
    q = mod("questions")
    assert Q283 not in q.R8_13_SKILL_ASSERTION_DROPPED
    assert Q283 not in q.SKILL_ASSERTION_DROPPED
    kq = find("KNOWLEDGE_QUESTIONS", "log_backends", "driver", "questions")
    assert Q283 not in kq
    assert kq == frozenset(C2_ALL)


# -- 283 spec ------------------------------------------------------------------------------


def test_283_log_question_spec() -> None:
    lb = mod("log_backends")
    spec = lb.LOG_QUESTIONS[Q283]
    assert spec.qid == Q283
    assert spec.backend == "kubernetes"
    assert spec.address is None
    assert spec.namespace is None
    assert spec.namespace_prefix == PREFIX
    assert tuple(spec.host_ports) == ()
    assert spec.needs_k8s is True
    assert spec.upstream_k8s_disabled is False
    assert lb.backend_of(Q283) == "kubernetes"
    # The new field defaults to None: every earlier entry is unchanged.
    for qid in (*C5_ALL, *C2_ALL):
        assert lb.LOG_QUESTIONS[qid].namespace_prefix is None, qid
    assert set(lb.LOG_QUESTIONS) == set(C5_ALL) | set(C2_ALL) | {Q283}
    fields = {f.name: f for f in dataclasses.fields(lb.LogQuestionSpec)}
    assert fields["namespace_prefix"].default is None


def test_backends_has_the_new_seams() -> None:
    names = {f.name for f in dataclasses.fields(mod("driver").Backends)}
    assert {"namespaces_with_prefix", "run_tool_names", "task_list_stats"} <= names


# -- namespaces_with_prefix (kubectl stand-in) ---------------------------------------------


def _kubectl(tmp_path: Path, body: str) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "kubectl"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    path = f"{bin_dir}:/usr/bin:/bin"
    return {"PATH": path, "HOME": str(tmp_path), "KUBECONFIG": str(tmp_path / "k")}


def _names(out: list[str] | None) -> list[str] | None:
    if out is None:
        return None
    return sorted(n.removeprefix("namespace/") for n in out)


def _nwp() -> Any:
    return find("namespaces_with_prefix", "log_backends", "driver")


def test_namespaces_with_prefix_lists_only_the_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listing = "\\n".join(
        ["namespace/default", "namespace/app-28-x", "namespace/app-2830",
         *(f"namespace/{n}" for n in NS_283), "namespace/kube-system"]
    )  # fmt: skip
    env = _kubectl(tmp_path, f'printf "{listing}\\n"\nexit 0\n')
    monkeypatch.setenv("PATH", env["PATH"])
    assert _names(_nwp()(PREFIX, env)) == sorted(NS_283)


def test_namespaces_with_prefix_empty_when_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _kubectl(
        tmp_path, 'printf "namespace/default\\nnamespace/app-94\\n"\nexit 0\n'
    )
    monkeypatch.setenv("PATH", env["PATH"])
    assert _nwp()(PREFIX, env) == []


@pytest.mark.parametrize(
    "body",
    [
        'echo "Unable to connect to the server" >&2\nexit 1\n',
        'printf "namespace/default\\n"\nexit 1\n',  # output but not exit 0
    ],
)
def test_namespaces_with_prefix_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    env = _kubectl(tmp_path, body)
    monkeypatch.setenv("PATH", env["PATH"])
    assert _nwp()(PREFIX, env) is None


def test_namespaces_with_prefix_without_kubectl_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "nobin"
    empty.mkdir()
    env = {"PATH": str(empty), "HOME": str(tmp_path)}
    monkeypatch.setenv("PATH", str(empty))
    assert _nwp()(PREFIX, env) is None


# -- todo_for and Config -------------------------------------------------------------------


def _questions(tmp_path: Path) -> tuple[Any, Any]:
    q = mod("questions")
    up = tmp_path / "up"
    write_283(up, expected="ANS-283")
    write_question(up, Q275, {"user_prompt": "p", "expected_output": ["ANS-275"]})
    return q.load_question(up, Q283), q.load_question(up, Q275)


def test_todo_for_three_modes(tmp_path: Path) -> None:
    todo_for = find("todo_for", "driver", "questions")
    on_q, off_q = _questions(tmp_path)
    assert on_q.enable_todo is True and off_q.enable_todo is False
    assert tuple(todo_for("question", on_q)) == (True, "question")
    assert tuple(todo_for("question", off_q)) == (False, "question")
    for q in (on_q, off_q):
        assert tuple(todo_for("on", q)) == (True, "forced")
        assert tuple(todo_for("off", q)) == (False, "forced")


def test_config_todo_mode_default(tmp_path: Path) -> None:
    driver = mod("driver")
    fields = {f.name: f for f in dataclasses.fields(driver.Config)}
    assert fields["todo_mode"].default == "question"
    assert _cfg(tmp_path).todo_mode == "question"
    assert _cfg(tmp_path, todo_mode="off").todo_mode == "off"


# -- environments --------------------------------------------------------------------------

BACKENDS = ("elasticsearch", "loki", "victorialogs", "kubernetes")


@pytest.mark.parametrize("backend", BACKENDS)
def test_tool_env_todo_switch(tmp_path: Path, backend: str) -> None:
    driver = mod("driver")
    cfg = _cfg(tmp_path)
    base = driver.tool_env(cfg, backend)
    assert TODO_ENV not in base
    off = driver.tool_env(cfg, backend, todo=False)
    assert json.dumps(off, sort_keys=True) == json.dumps(base, sort_keys=True)
    assert list(off) == list(base)
    assert driver.tool_env(cfg, backend, todo=True) == {**base, TODO_ENV: "1"}
    know = driver.tool_env(cfg, backend, knowledge=True)
    assert driver.tool_env(cfg, backend, knowledge=True, todo=True) == {
        **know,
        TODO_ENV: "1",
    }


@pytest.mark.usefixtures("fixed_hash")
@pytest.mark.parametrize("backend", BACKENDS)
def test_web_and_worker_defaults_byte_identical(tmp_path: Path, backend: str) -> None:
    """B1/C5/C2 envs with ``todo`` at its default are byte for byte the
    envs with ``todo=False``, and carry no switch."""
    driver = mod("driver")
    cfg = _cfg(tmp_path)
    pg, k8s = _pg(), _k8s(tmp_path)
    acct = _account("holmes-eval-todo-a", "pw-fake-11")
    ident = tmp_path / "ids.json"
    product_es = {"OPSPILOT_ES_URL": cfg.es_url, "OPSPILOT_ES_API_KEY": "ro-fake-3"}
    product_es = product_es if backend == "elasticsearch" else {}
    web0 = driver.web_env(cfg, pg, ident, acct, backend=backend)
    web1 = driver.web_env(cfg, pg, ident, acct, backend=backend, todo=False)
    assert json.dumps(web1) == json.dumps(web0)
    wk0 = driver.worker_env(cfg, pg, product_es, k8s, None, SECRETS, backend=backend)
    wk1 = driver.worker_env(
        cfg, pg, product_es, k8s, None, SECRETS, backend=backend, todo=False
    )
    assert json.dumps(wk1) == json.dumps(wk0)
    for env in (web0, wk0):
        assert TODO_ENV not in env


@pytest.mark.usefixtures("fixed_hash")
@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("knowledge", [False, True])
def test_todo_on_both_web_and_worker(
    tmp_path: Path, backend: str, knowledge: bool
) -> None:
    driver = mod("driver")
    cfg = _cfg(tmp_path)
    pg, k8s = _pg(), _k8s(tmp_path)
    acct = _account("holmes-eval-todo-b", "pw-fake-12")
    ident = tmp_path / "ids.json"
    product_es = (
        {"OPSPILOT_ES_URL": cfg.es_url, "OPSPILOT_ES_API_KEY": "ro-fake-3"}
        if backend == "elasticsearch"
        else {}
    )
    kw: dict[str, Any] = {"backend": backend, "knowledge": knowledge}
    web = driver.web_env(cfg, pg, ident, acct, todo=True, **kw)
    worker = driver.worker_env(cfg, pg, product_es, k8s, None, SECRETS, todo=True, **kw)
    assert web[TODO_ENV] == "1" and worker[TODO_ENV] == "1"
    plain_web = driver.web_env(cfg, pg, ident, acct, **kw)
    plain_worker = driver.worker_env(cfg, pg, product_es, k8s, None, SECRETS, **kw)
    assert web == {**plain_web, TODO_ENV: "1"}
    assert worker == {**plain_worker, TODO_ENV: "1"}
    shared = driver.tool_env(cfg, backend, knowledge=knowledge, todo=True)
    for key, value in shared.items():
        assert web[key] == value and worker[key] == value, key
    driver.check_env_isolation(worker, "engineer-fake-value-1", role="worker")
    driver.check_env_isolation(web, "engineer-fake-value-1", role="web")


# -- task_list_stats_from_rows -------------------------------------------------------------


def _stats_fn() -> Any:
    return find("task_list_stats_from_rows", "driver", "tool_calls", "log_backends")


def test_task_list_stats_counts() -> None:
    first = todos("pending", "pending", "pending")
    later = todos("completed", "completed", "failed", "in_progress", "pending")
    steps = [
        # Stored out of order on purpose; later sequence holds the last list.
        step(3, STEP_C, [
            query_ok(0, "ev-4"),
            todo_result(1, later),
            query_refused(2, "elasticsearch_search", "denied", "DUPLICATE_TOOL_CALL"),
        ]),
        step(1, STEP_A, [
            todo_result(0, first),
            query_ok(1, "ev-1", "elasticsearch_list_indices"),
            query_ok(2, "ev-2"),
        ]),
        step(2, STEP_B, [
            todo_result(0, [{"id": "1", "content": "x", "status": "bogus"}]),
            query_refused(1, "kubernetes_pods", "timeout", "SOURCE_TIMEOUT"),
            query_ok(2, "ev-3"),
        ]),
        # A late_result history row: skipped entirely.
        step(4, STEP_L, [
            todo_result(0, todos("completed")),
            query_ok(1, "ev-late"),
        ], status="late_result"),
    ]  # fmt: skip
    out = _stats_fn()(steps)
    assert out["calls_ok"] == 2
    assert out["calls_refused"] == 1
    # ev-1..ev-4, the timeout and the duplicate; TodoWrite never counted.
    assert out["queries"] == 6
    assert out["last"] == {
        "total": 5, "completed": 2, "failed": 1, "in_progress": 1, "pending": 1,
    }  # fmt: skip
    assert set(out) == {"calls_ok", "calls_refused", "queries", "last"}


def test_task_list_stats_same_step_later_ordinal_wins() -> None:
    steps = [
        step(1, STEP_A, [
            todo_result(2, todos("completed", "completed")),
            todo_result(0, todos("pending", "pending", "pending")),
            query_ok(1, "ev-1"),
        ]),
    ]  # fmt: skip
    out = _stats_fn()(steps)
    assert out["calls_ok"] == 2 and out["queries"] == 1
    assert out["last"] == {
        "total": 2, "completed": 2, "failed": 0, "in_progress": 0, "pending": 0,
    }  # fmt: skip


def test_task_list_stats_refused_does_not_replace_last() -> None:
    steps = [
        step(1, STEP_A, [todo_result(0, todos("in_progress", "pending"))]),
        step(2, STEP_B, [todo_result(0, "not a list")]),
    ]
    out = _stats_fn()(steps)
    assert (out["calls_ok"], out["calls_refused"], out["queries"]) == (1, 1, 0)
    assert out["last"] == {
        "total": 2, "completed": 0, "failed": 0, "in_progress": 1, "pending": 1,
    }  # fmt: skip


def test_task_list_stats_without_any_list() -> None:
    fn = _stats_fn()
    assert fn([]) == {"calls_ok": 0, "calls_refused": 0, "queries": 0, "last": None}
    only_refused = [step(1, STEP_A, [todo_result(0, "bad"), query_ok(1, "ev-1")])]
    assert fn(only_refused) == {
        "calls_ok": 0, "calls_refused": 1, "queries": 1, "last": None,
    }  # fmt: skip
    # An empty list is a saved (ok) list with zero tasks.
    emptied = [step(1, STEP_A, [todo_result(0, todos("pending")), todo_result(1, [])])]
    assert fn(emptied)["last"] == {
        "total": 0, "completed": 0, "failed": 0, "in_progress": 0, "pending": 0,
    }  # fmt: skip
    queries_only = [step(1, STEP_A, [query_ok(0, "ev-1"), query_ok(1, "ev-2")])]
    assert fn(queries_only) == {
        "calls_ok": 0, "calls_refused": 0, "queries": 2, "last": None,
    }  # fmt: skip


def test_tool_named_todowrite_with_evidence_is_a_query() -> None:
    """Only results without an ``evidence_id`` are the task list."""
    steps = [step(1, STEP_A, [query_ok(0, "ev-9", "TodoWrite")])]
    out = _stats_fn()(steps)
    assert (out["calls_ok"], out["calls_refused"], out["queries"]) == (0, 0, 1)


# -- the include_tool_calls attachment -----------------------------------------------------


def _attachment_rows() -> list[dict[str, Any]]:
    plan = [
        {"id": "1", "content": f"{TODO_SENTINEL} password=hunter2-fake-77",
         "status": "in_progress"},
        {"id": "2", "content": "read the pods", "status": "pending"},
    ]  # fmt: skip
    bad = [{"id": "1", "content": "x", "status": "bogus"}]
    return [
        step(
            1,
            STEP_A,
            [todo_result(0, plan), query_ok(1, "ev-1", "kubernetes_pods")],
            calls=[("TodoWrite", {"todos": plan}), ("kubernetes_pods", {"i": 1})],
        ),
        step(
            2,
            STEP_B,
            [todo_result(0, bad)],
            calls=[("TodoWrite", {"todos": bad})],
        ),
    ]


def _views() -> dict[str, dict[str, Any]]:
    return {
        "ev-1": {
            "evidence_id": "ev-1", "status": "ok", "adopted": True,
            "tool": "kubernetes_pods", "tool_version": "1", "source": "kubernetes",
            "target_id": "m0-otel-20260909", "query": {"namespace": "app-283-alpha"},
            "content": [{"name": "checkout-1"}],
        }
    }  # fmt: skip


def test_successful_todowrite_is_a_call_with_output() -> None:
    steps = _attachment_rows()
    calls = mod("tool_calls").tool_calls_from_rows(steps, _views())
    assert [c.tool for c in calls] == ["TodoWrite", "kubernetes_pods", "TodoWrite"]
    ok, pods, refused = calls
    stored = steps[0]["tool_results"][0]["result"]
    assert ok.refused_code is None and ok.refused_reason is None
    assert ok.output is not None
    assert ok.output["status"] == "ok"
    assert ok.output["todos"] == stored["todos"]
    assert "evidence_id" not in ok.output
    params = dict(ok.params)
    assert [t["id"] for t in params["todos"]] == ["1", "2"]
    assert TODO_SENTINEL in json.dumps(params)
    # Redacted model arguments (C1 section 1: content goes through
    # redact_credentials).
    assert "hunter2-fake-77" not in json.dumps(params)
    assert "hunter2-fake-77" not in json.dumps(ok.output)
    assert pods.output == _views()["ev-1"] and pods.refused_code is None
    assert (refused.refused_code, refused.refused_reason) == (
        "denied",
        "INVALID_TODOS",
    )
    assert refused.output is None


def test_todowrite_in_formatted_attachment() -> None:
    calls = mod("tool_calls").tool_calls_from_rows(_attachment_rows(), _views())
    block = mod("text").format_tool_calls(calls)
    lines = [ln for ln in block.splitlines() if ln.startswith("* Tool #")]
    assert len(lines) == 3
    assert lines[0].startswith("* Tool #1: TodoWrite(")
    assert TODO_SENTINEL in block
    assert "INVALID_TODOS" in block
    assert "hunter2-fake-77" not in block


# -- command line --------------------------------------------------------------------------


class _Stop(Exception):
    pass


def _main_cfg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, extra: list[str]
) -> Any:
    driver, questions = mod("driver"), mod("questions")
    monkeypatch.setattr(questions, "verify_upstream", lambda root: None)
    if hasattr(driver, "verify_upstream"):
        monkeypatch.setattr(driver, "verify_upstream", lambda root: None)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    seen: list[Any] = []

    def stop(cfg: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(cfg)
        raise _Stop

    monkeypatch.setattr(driver, "prepare_round", stop)
    monkeypatch.setattr(driver, "run_round", stop)
    env_file = tmp_path / "secrets.env"
    env_file.write_text("DEEPSEEK_API_KEY=model-fake-value-4\n")
    env_file.chmod(0o600)
    argv = [
        command, "--round", "t1", "--upstream", str(tmp_path), "--questions", "275",
        "--env-file", str(env_file), "--trace", "off", "--no-judge", "--allow-dirty",
        "--tmp-root", str(tmp_path / "tmp"), "--evidence-root", str(tmp_path / "ev"),
        *extra,
    ]  # fmt: skip
    with pytest.raises(_Stop):
        driver.main(argv)
    assert seen
    return seen[-1]


@pytest.mark.parametrize("command", ["round", "swing"])
@pytest.mark.parametrize("mode", ["question", "on", "off"])
def test_cli_todo_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, mode: str
) -> None:
    cfg = _main_cfg(tmp_path, monkeypatch, command, ["--todo", mode])
    assert cfg.todo_mode == mode


@pytest.mark.parametrize("command", ["round", "swing"])
def test_cli_todo_default_is_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    assert _main_cfg(tmp_path, monkeypatch, command, []).todo_mode == "question"


def test_cli_todo_rejects_other_values(tmp_path: Path) -> None:
    driver = mod("driver")
    with pytest.raises(SystemExit) as caught:
        driver.main(
            ["round", "--round", "t1", "--upstream", str(tmp_path), "--todo", "yes"]
        )
    assert caught.value.code == 2
