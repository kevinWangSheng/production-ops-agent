"""Implementer unit tests for #666 PR-D (C2 knowledge questions); no PG.

The independent test author writes the contract tests; these pin the
implementation's own seams with in-process doubles: the C2 environments,
and one C2 trial through import, catalog check, revocation and every
fail-closed point.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts.holmes_evals import driver, intake, knowledge, log_backends, questions
from scripts.holmes_evals.setup import ScriptResult, log_setup_env
from scripts.itbench_lite.services import Started

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
Q94 = "94_skill_transparency"
Q162 = "162_get_skills"
Q89 = "89_skill_missing_cloudwatch"


class _Pg:
    owner_dsn = "postgresql://owner@127.0.0.1:1/x"
    runtime_dsn = "postgresql://worker@127.0.0.1:1/x"
    web_dsn = "postgresql://web@127.0.0.1:1/x"


def _cfg(tmp_path: Path) -> driver.Config:
    cfg = driver.Config(
        round="d1", upstream=tmp_path / "up", tmp_root=tmp_path / "tmp",
        evidence_root=tmp_path / "ev",
    )  # fmt: skip
    cfg.secrets = {"DEEPSEEK_API_KEY": "model-key-x", "ZAI_API_KEY": "judge-key-x"}
    cfg.k8s = driver.K8sTarget("https://127.0.0.1:29243", str(tmp_path / "ca"), "tok")
    cfg.account = intake.new_account()
    cfg.import_account = intake.UiAccount("holmes-eval-import-x", "import-pw-x")
    return cfg


def test_question_lists_and_specs() -> None:
    assert questions.QUESTION_GROUPS["c2"] == questions.C2_QUESTIONS
    assert questions.ALL_QUESTIONS[-6:] == questions.C2_QUESTIONS
    assert driver.resolve_questions("c2") == questions.C2_QUESTIONS
    assert driver.resolve_questions("162") == (Q162,)
    assert log_backends.KNOWLEDGE_QUESTIONS == set(questions.C2_QUESTIONS)
    for qid in questions.C2_QUESTIONS:
        assert log_backends.backend_of(qid) == "kubernetes"
        assert qid not in questions.R8_13_SKILL_ASSERTION_DROPPED
    assert log_backends.LOG_QUESTIONS[Q94].namespace == "app-94"
    assert not log_backends.LOG_QUESTIONS[Q89].needs_k8s


def test_kubernetes_envs_with_the_switch_on_both_sides(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert cfg.account is not None and cfg.k8s is not None
    web = driver.web_env(
        cfg, _Pg(), tmp_path / "ids", cfg.account, backend="kubernetes",
        knowledge=True, import_account=cfg.import_account,
    )  # fmt: skip
    worker = driver.worker_env(
        cfg, _Pg(), {}, cfg.k8s, None, cfg.secrets, backend="kubernetes",
        knowledge=True,
    )  # fmt: skip
    for env in (web, worker):
        assert env["OPSPILOT_KNOWLEDGE_TOOL"] == "1"
        assert not any(k.startswith(("OPSPILOT_ES_", "OPSPILOT_LOKI_")) for k in env)
    assert web["OPSPILOT_UI_USERS"].count("=") == 2
    assert "holmes-eval-import-x=" in web["OPSPILOT_UI_USERS"]
    assert "import-pw-x" not in json.dumps(web)
    driver.check_env_isolation(web, "", role="web")
    driver.check_env_isolation(worker, "", role="worker")
    # B1 bytes unchanged without the switch.
    plain = driver.web_env(cfg, _Pg(), tmp_path / "ids", cfg.account)
    assert "OPSPILOT_KNOWLEDGE_TOOL" not in plain and "OPSPILOT_ES_URL" in plain
    env = log_setup_env(
        log_backends.LOG_QUESTIONS[Q94],
        victorialogs_url="http://127.0.0.1:29428",
        kubeconfig=tmp_path / "kc",
        environ={"PATH": "/bin", "TZ": "Asia/Tokyo"},
    )
    assert env == {
        "PATH": "/bin",
        "TZ": "Asia/Tokyo",
        "KUBECONFIG": str(tmp_path / "kc"),
    }
    assert cfg.log_mode("kubernetes") == "none"
    assert cfg.ports.log_ports("kubernetes", "none") == {}


def test_disclosures() -> None:
    q = questions.Question(user_prompt="x")
    shift = driver.ShiftRecord(
        applied=False, reason="RELATIVE_TIME", backend="kubernetes"
    )
    codes = driver.disclosures(Q162, q, shift, k8s_namespace="otel-demo")
    assert {"RUNBOOKS_IMPORTED_REVIEWED", "GLOBAL_INSTRUCTIONS_NOT_IMPORTED",
            "K8S_SCOPE_DIFFERS"} <= set(codes)  # fmt: skip
    assert "SKILL_ASSERTION_DROPPED" not in codes
    assert "GLOBAL_INSTRUCTIONS_NOT_IMPORTED" not in driver.disclosures(
        Q94, q, shift, k8s_namespace="otel-demo"
    )


def test_catalog_matches() -> None:
    ok = {
        "status": "ok",
        "omitted_for_size": 0,
        "entries": [{"skill_id": "a"}, {"skill_id": "b"}],
    }
    assert knowledge.catalog_matches(ok, ["b", "a"])
    assert not knowledge.catalog_matches(ok | {"status": "unavailable"}, ["a", "b"])
    assert not knowledge.catalog_matches(ok | {"omitted_for_size": 1}, ["a", "b"])
    assert not knowledge.catalog_matches(ok, ["a"])
    assert knowledge.catalog_summary(ok) == {
        "status": "ok", "skill_ids": ["a", "b"], "omitted_for_size": 0,
    }  # fmt: skip
    assert not knowledge.catalog_matches(None, [])


# -- one C2 trial through doubles ---------------------------------------------------


def _question(root: Path, qid: str) -> Path:
    d = root / questions.QUESTIONS_DIR / qid
    skill = d / "database-troubleshooting"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        '---\nname: database-troubleshooting\ndescription: "DB"\n---\n1. check\n'
    )
    (d / "test_case.yaml").write_text(
        json.dumps(
            {
                "user_prompt": "Why does blue-whale time out?",
                "before_test": "echo load",
                "after_test": "echo clean",
                "expected_output": ["SYNTHETIC-ANSWER-ELEMENT"],
            }
        )
    )
    return d


def _report() -> dict[str, Any]:
    return {
        "schema_version": "m0-report-v2",
        "assessment_status": "completed",
        "conclusion": "supported",
        "summary": "The database pool is exhausted.",
        "claims": [
            {"kind": "fact", "text": "errors", "evidence_ids": ["ev-1"],
             "target_refs": ["t-1"], "time_scope_ref": "w-1"}
        ],
        "gaps": [],
        "next_steps": [],
    }  # fmt: skip


class _Workbench:
    """The runbook store as the doubles see it: what is active."""

    def __init__(self, *, residue: list[str] | None = None) -> None:
        self.active: list[str] = list(residue or [])
        self.calls: list[str] = []

    def import_runbooks(self, url: str, account: Any, directory: Path, target: str):
        self.calls.append(f"import:{account.name}:{target}")
        self.active.append("database-troubleshooting")
        return [{"name": "database-troubleshooting", "entry_id": "e-1",
                 "revision": 1, "entry_generation": 1}], []  # fmt: skip

    def revoke_runbooks(self, url: str, account: Any, rows: Any) -> list[str]:
        self.calls.append("revoke")
        self.active = [n for n in self.active if n not in {r["name"] for r in rows}]
        return []


def _backends(
    seen: dict[str, Any], wb: _Workbench, catalog: dict[str, Any] | None
) -> driver.Backends:
    order: list[str] = seen.setdefault("order", [])

    def run_script(script: str | None, **kw: Any) -> ScriptResult:
        order.append(kw["operation"])
        stamp = NOW.isoformat()
        return ScriptResult(kw["operation"], 0, False, stamp, stamp, 0.1, "0" * 64)

    def start(name: str) -> Any:
        def started(*a: Any, **k: Any) -> Started:
            order.append(name)
            seen["env_" + name] = a[0]
            return Started(name, lambda: order.append("stop-" + name))

        return started

    def submit(web_url: str, account: Any, **kw: Any) -> intake.SubmitResult:
        order.append("submit")
        return intake.SubmitResult(
            201, "inc-1", "00000000-0000-0000-0000-000000000001", False, None
        )

    def judge(job: Any) -> int:
        Path(job["out"]).write_text(json.dumps({"choice": "A", "passed": True,
            "error_code": None, "attempts": 1, "prompt_sha256": "p", "request_bytes": 9}))  # fmt: skip
        return 0

    def importer(*a: Any) -> Any:
        order.append("import")
        return wb.import_runbooks(*a)

    def revoker(*a: Any) -> Any:
        order.append("revoke")
        return wb.revoke_runbooks(*a)

    return driver.Backends(
        start_pg=lambda d, p: _Pg(),
        stop_pg=lambda pg: None,
        start_web=start("web"),
        start_worker=start("worker"),
        run_script=run_script,
        submit=submit,
        run_state=lambda dsn, run: "completed",
        collect=lambda dsn, run: (
            {"report": _report(), "outcome": {"report_available": True}},
            {"r": 1},
        ),
        read_tool_calls=lambda dsn, run: [],
        trace_roots=lambda p, r, e: {"project": None, "read_back": "x", "roots": []},
        judge=judge,
        port_in_use=lambda port: False,
        namespace_gone=lambda ns, env: True,
        import_runbooks=importer,
        revoke_runbooks=revoker,
        active_runbooks=lambda dsn, target: sorted(wb.active),
        run_catalog=lambda dsn, run: catalog,
        now=lambda: NOW,
        sleep=lambda s: None,
    )


_OK = {
    "status": "ok",
    "omitted_for_size": 0,
    "entries": [{"skill_id": "database-troubleshooting", "revision": 1}],
}
_OK_SUMMARY = {
    "status": "ok",
    "skill_ids": ["database-troubleshooting"],
    "omitted_for_size": 0,
}


def test_a_c2_trial_imports_checks_and_revokes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPSPILOT_LEDGER_DIR", str(tmp_path / "ledger"))
    cfg = _cfg(tmp_path)
    cfg.questions = (Q94,)
    _question(cfg.upstream, Q94)
    seen: dict[str, Any] = {}
    wb = _Workbench()
    out = driver.run_question(cfg, _backends(seen, wb, _OK), Q94, [1])
    assert out["trials"][1]["status"] == "completed", out
    order = seen["order"]
    assert order.index("web") < order.index("import") < order.index("worker")
    assert order.index("submit") < order.index("revoke") < order.index("stop-web")
    assert (
        wb.active == []
        and wb.calls[0] == f"import:holmes-eval-import-x:{intake.TARGET_ID}"
    )
    assert seen["env_worker"]["OPSPILOT_KNOWLEDGE_TOOL"] == "1"
    assert seen["env_web"]["OPSPILOT_KNOWLEDGE_TOOL"] == "1"
    block = json.loads(cfg.summary_path(Q94, 1).read_text())["holmes_eval"]
    k = block["knowledge"]
    assert k["active_before_import"] == [] and k["active_after_revoke"] == []
    assert k["active_after_import"] == ["database-troubleshooting"]
    assert (
        k["run_catalog"] == _OK_SUMMARY
        and k["revoked"] == 1
        and k["catalog_retries"] == 0
    )
    assert k["fixtures"][0]["kind"] == "skill_md" and "content" not in k["fixtures"][0]
    assert block["data_plane"]["backend"] == "kubernetes"
    assert block["shift"]["reason"] == "RELATIVE_TIME"
    raw = cfg.summary_path(Q94, 1).read_text()
    assert "import-pw-x" not in raw and "1. check" not in raw


def test_residue_before_import_aborts_the_round(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.questions = (Q94,)
    _question(cfg.upstream, Q94)
    b = _backends({}, _Workbench(residue=["left-over"]), _OK)
    with pytest.raises(driver.RoundAbort) as err:
        driver.run_question(cfg, b, Q94, [1])
    assert err.value.code == "RUNBOOK_RESIDUE"


def test_a_catalog_mismatch_is_retried_and_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPSPILOT_LEDGER_DIR", str(tmp_path / "ledger"))
    cfg = _cfg(tmp_path)
    cfg.questions = (Q94,)
    _question(cfg.upstream, Q94)
    wb = _Workbench()
    seen: dict[str, Any] = {}
    bad = {"status": "unavailable", "omitted_for_size": 0, "entries": []}
    out = driver.run_question(cfg, _backends(seen, wb, bad), Q94, [1])
    assert out["trials"][1]["status"] == "missing"
    summary = json.loads(cfg.summary_path(Q94, 1).read_text())["holmes_eval"]
    assert [h["code"] for h in summary["history"]] == ["RUNBOOK_CATALOG_MISMATCH"] * 3
    # Every failed attempt still revoked what it imported.
    assert wb.active == [] and seen["order"].count("revoke") == 3


def test_an_import_refusal_is_an_infra_failure(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.questions = (Q94,)
    _question(cfg.upstream, Q94)
    wb = _Workbench()
    b = _backends({}, wb, _OK)
    b.import_runbooks = lambda *a: ([], ["submit database-troubleshooting: 409"])
    attempt = driver.Attempt(
        cfg, b, Q94, questions.load_question(cfg.upstream, Q94), 1, 1, []
    )
    with pytest.raises(driver.InfraFailure) as err:
        attempt.run()
    assert err.value.code == "RUNBOOK_IMPORT_FAILED"


def test_a_revoke_refusal_after_the_run_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPSPILOT_LEDGER_DIR", str(tmp_path / "ledger"))
    cfg = _cfg(tmp_path)
    cfg.questions = (Q94,)
    _question(cfg.upstream, Q94)
    b = _backends({}, _Workbench(), _OK)
    b.revoke_runbooks = lambda *a: ["revoke database-troubleshooting: 409"]
    with pytest.raises(driver.RoundAbort) as err:
        driver.run_question(cfg, b, Q94, [1])
    assert (err.value.code, err.value.detail) == ("CLEANUP_FAILED", "runbook revoke")
