"""#666 PR-D contract: the Holmes comparison side (``holmes_compare``) for
the six C2 questions.

Independent contract tests written from the task record's "PR-D 接口"
section ("Holmes 对照") without reading the PR-D implementation. No real
pytest harness, kubectl, model or judge runs: ``runner.subprocess.run`` is a
stand-in that writes what upstream's plugin and the judge process would
(``harness.json``, ``judge-input.txt``, ``judge-result.json``); namespace
and port checks are recorders. Question directories are synthetic.

Rules under test:

* ``runner.preflight`` accepts the C2 questions; 94/95/96/162 need
  ``--kubeconfig``, 89/90 do not; no ES variables are required;
* ``pytest_env`` for C2: no ES/Loki/VictoriaLogs variables,
  ``OHC_SHIFT_BACKEND=none``, ``ASK_HOLMES_TEST_TYPE=cli`` (the question's
  own ``test_type`` is upstream's business), ``OHC_DROP_SKILL=0``, no
  workbench or import variables;
* one trial: the namespace release check before and after, no ES purge,
  fixtures untouched; the summary's ``data_plane`` (``kubernetes``,
  ``upstream-direct``), ``run_mode`` (``run_mode()`` or ``cli``), the
  disclosure ``KNOWLEDGE_UPSTREAM_DIRECT``, ``HOLMES_NO_SKILL_CATALOG`` only
  for ``cli`` without a ``SKILL.md``; no answer or runbook body;
* ``runner round --questions c2`` selects the six;
* ``merge --questions c2`` builds the table from OpsPilot rows whose
  ``data_plane.mode`` is ``none`` (not ``DATA_PLANE_DIFFERS``).

Assumptions beyond the interface (also marked ``# ASSUMPTION:``):

* A selection with a k8s C2 question and no kubeconfig is a preflight
  ``RoundAbort`` (code not pinned); a kubeconfig whose cluster is absent is
  found only when the attempt runs (env_broken).
* ``run_mode`` is a top-level key of the ``holmes_compare`` block.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from tests.holmes_evals_d_support import (
    BODY_SENTINEL,
    C2_ALL,
    C2_K8S,
    C2_NO_K8S,
    GLOBAL_SENTINEL,
    Q89,
    Q94,
    Q162,
    write_c2_question,
)
from tests.test_holmes_compare_contract import POISON, holmes_summary, ops_summary


def _runner() -> Any:
    from scripts.holmes_compare import runner

    return runner


def _cfg(tmp_path: Path, questions: tuple[str, ...], **over: Any) -> Any:
    runner = _runner()
    python = tmp_path / "holmes-python"
    python.write_text("#!/bin/sh\n")
    wanted: dict[str, Any] = {
        "round": "t1",
        "upstream": tmp_path / "upstream",
        "holmes_python": python,
        "judge_path": "opspilot",
        "questions": questions,
        "secrets": {"DEEPSEEK_API_KEY": POISON[2], "ZAI_API_KEY": POISON[3]},
        "tmp_root": tmp_path / "tmp",
        "evidence_root": tmp_path / "evidence",
        **over,
    }
    return runner.Config(**wanted)


@pytest.fixture
def pinned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    runner = _runner()
    up = tmp_path / "upstream"
    up.mkdir(exist_ok=True)
    monkeypatch.setattr(runner, "verify_upstream", lambda root: None)
    return up


# -- preflight ---------------------------------------------------------------------------


def test_preflight_accepts_c2_with_kubeconfig(tmp_path: Path, pinned: Path) -> None:
    kube = tmp_path / "kubeconfig"
    kube.write_text("apiVersion: v1\n")
    _runner().preflight(_cfg(tmp_path, C2_ALL, kubeconfig=kube))


def test_preflight_89_90_need_no_cluster(tmp_path: Path, pinned: Path) -> None:
    _runner().preflight(_cfg(tmp_path, C2_NO_K8S))


@pytest.mark.parametrize("qid", C2_K8S)
def test_preflight_k8s_c2_needs_kubeconfig(
    tmp_path: Path, pinned: Path, qid: str
) -> None:
    runner = _runner()
    with pytest.raises(runner.RoundAbort) as caught:
        runner.preflight(_cfg(tmp_path, (Q89, qid)))
    assert caught.value.code != "QUESTION_UNKNOWN"
    kube = tmp_path / "kubeconfig"
    kube.write_text("apiVersion: v1\n")
    runner.preflight(_cfg(tmp_path, (Q89, qid), kubeconfig=kube))


# -- harness environment ----------------------------------------------------------------


def test_pytest_env_for_c2(tmp_path: Path) -> None:
    runner = _runner()
    kube = tmp_path / "kubeconfig"
    cfg = _cfg(tmp_path, C2_ALL, kubeconfig=kube)
    for qid in C2_ALL:
        env = runner.pytest_env(cfg, qid, tmp_path)
        assert env["OHC_QID"] == qid
        assert env["OHC_SHIFT_BACKEND"] == "none"
        assert env["ASK_HOLMES_TEST_TYPE"] == "cli"
        assert env["OHC_DROP_SKILL"] == "0"
        assert env["KUBECONFIG"] == str(kube)
        for name in ("ELASTICSEARCH_URL", "ELASTICSEARCH_API_KEY", "OHC_LOKI_URL",
                     "VICTORIALOGS_URL"):  # fmt: skip
            assert name not in env, (qid, name)
        assert not [k for k in env if k.startswith(("OPSPILOT_", "LANGSMITH"))]
        assert POISON[3] not in env.values()  # judge key stays out (opspilot path)
    no_kube = runner.pytest_env(_cfg(tmp_path, C2_NO_K8S), Q89, tmp_path)
    assert "KUBECONFIG" not in no_kube


def test_round_cli_selects_c2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner()
    seen: list[Any] = []
    monkeypatch.setattr(runner, "run_round", lambda cfg: seen.append(cfg) or [])
    monkeypatch.setattr(
        runner,
        "load_env_file",
        lambda path: {"DEEPSEEK_API_KEY": POISON[2], "ZAI_API_KEY": POISON[3]},
    )
    base = [
        "round", "--round", "r1", "--upstream", str(tmp_path),
        "--holmes-python", str(tmp_path / "py"), "--env-file", str(tmp_path / "e"),
    ]  # fmt: skip
    assert runner.main([*base, "--questions", "c2"]) == 0
    assert tuple(seen[-1].questions) == C2_ALL
    assert runner.main([*base, "--questions", "162"]) == 0
    assert tuple(seen[-1].questions) == (Q162,)


# -- one trial, stand-in harness ---------------------------------------------------------


class FakeHost:
    """``subprocess.run`` for pytest and the judge, namespace/port checks."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.ns_present: dict[str, bool] = {}
        self.ns_checks: list[str] = []
        self.events: list[str] = []

    def run(self, argv, *args: Any, **kwargs: Any) -> Any:
        import subprocess

        argv = [str(a) for a in argv]
        env = dict(kwargs.get("env") or {})
        self.calls.append({"argv": argv, "env": env, "cwd": kwargs.get("cwd")})
        if "pytest" in argv:
            self.events.append("pytest")
            trial = Path(env["OHC_TRIAL_DIR"])
            trial.mkdir(parents=True, exist_ok=True)
            (trial / "judge-input.txt").write_text("Holmes answered something\n")
            (trial / "question-original.txt").write_text("q")
            (trial / "question-delivered.txt").write_text("q")
            (trial / "harness.json").write_text(
                json.dumps(
                    {
                        "selected": 1,
                        "captured": True,
                        "shift": {"status": "ok", "applied": False},
                        "properties": {
                            "holmes_duration": 12.5,
                            "tool_call_count": 3,
                            "num_llm_calls": 2,
                        },
                        "phases": {},
                        "errors": {},
                    }
                )
            )
        elif "scripts.holmes_evals.judge_run" in argv:
            self.events.append("judge")
            out = Path(argv[argv.index("--out") + 1])
            out.write_text(
                json.dumps(
                    {"choice": "A", "passed": True, "error_code": None, "attempts": 1,
                     "prompt_sha256": "0" * 64}
                )
            )  # fmt: skip
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def namespace_gone(self, namespace: str, env: Any) -> bool:
        self.events.append(f"ns:{namespace}")
        self.ns_checks.append(namespace)
        return not self.ns_present.get(namespace, False)


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch, pinned: Path) -> FakeHost:
    runner = _runner()
    fake = FakeHost()
    monkeypatch.setattr(runner.subprocess, "run", fake.run)
    monkeypatch.setattr(runner, "namespace_gone", fake.namespace_gone)
    monkeypatch.setattr(runner, "port_in_use", lambda port: False)

    def no_es(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("ES touched for a C2 question")

    import scripts.holmes_evals.setup as setup_mod
    import scripts.holmes_evals.shift as shift_mod

    monkeypatch.setattr(setup_mod, "purge_question_indices", no_es)
    monkeypatch.setattr(shift_mod, "EsAdmin", no_es)
    monkeypatch.setattr(runner, "start_victorialogs", no_es)
    return fake


def _snapshot(qdir: Path) -> dict[str, str]:
    return {
        str(p.relative_to(qdir)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(qdir.rglob("*"))
        if p.is_file()
    }


def _trial(tmp_path: Path, qid: str) -> tuple[dict, str]:
    runner = _runner()
    kube = tmp_path / "kubeconfig"
    kube.write_text("apiVersion: v1\n")
    cfg = _cfg(tmp_path, (qid,), kubeconfig=kube)
    summary = runner.run_trial(cfg, qid, 1)
    raw = cfg.summary_path(qid, 1).read_text(encoding="utf-8")
    return summary, raw


def test_c2_k8s_trial_summary(tmp_path: Path, host: FakeHost, pinned: Path) -> None:
    qdir = write_c2_question(pinned, Q94, expected=POISON[0])
    before = _snapshot(qdir)
    summary, raw = _trial(tmp_path, Q94)
    assert summary["status"] == "completed"
    block = summary["holmes_compare"]
    assert block["category"] == "ok"
    # Release check before and after the harness; no ES purge.
    pytest_at = host.events.index("pytest")
    checks = [i for i, e in enumerate(host.events) if e == "ns:app-94"]
    assert checks and checks[0] < pytest_at < checks[-1]
    (call,) = [c for c in host.calls if "pytest" in c["argv"]]
    assert call["env"]["OHC_SHIFT_BACKEND"] == "none"
    assert Path(call["cwd"]) == pinned
    plane = block["data_plane"]
    assert plane["backend"] == "kubernetes" and plane["mode"] == "upstream-direct"
    assert block["run_mode"] == "cli"  # no test_type key -> cli
    codes = block["disclosures"]
    assert "KNOWLEDGE_UPSTREAM_DIRECT" in codes
    assert "HOLMES_NO_SKILL_CATALOG" not in codes  # SKILL.md present
    assert "SKILL_ASSERTION_DROPPED" not in codes
    assert "LOKI_ORIGINALS_KEPT" not in codes
    # Fixtures untouched; no answer, body or key in the evidence.
    assert _snapshot(qdir) == before
    for needle in (*POISON, BODY_SENTINEL):
        assert needle not in raw


def test_c2_no_namespace_trial(tmp_path: Path, host: FakeHost, pinned: Path) -> None:
    write_c2_question(pinned, Q89, expected=POISON[0])
    summary, raw = _trial(tmp_path, Q89)
    assert summary["status"] == "completed"
    assert host.ns_checks == []
    block = summary["holmes_compare"]
    assert block["run_mode"] == "cli"
    assert "KNOWLEDGE_UPSTREAM_DIRECT" in block["disclosures"]
    assert "HOLMES_NO_SKILL_CATALOG" not in block["disclosures"]


@pytest.mark.parametrize(
    ("test_type", "mode", "flagged"),
    [(None, "cli", True), ("cli", "cli", True), ("cluster", "cluster", False),
     ("server", "server", False)],
)  # fmt: skip
def test_162_run_mode_and_skill_catalog_disclosure(
    tmp_path: Path,
    host: FakeHost,
    pinned: Path,
    test_type: str | None,
    mode: str,
    flagged: bool,
) -> None:
    qdir = write_c2_question(pinned, Q162, expected=POISON[0])
    doc_path = qdir / "test_case.yaml"
    import yaml

    doc = yaml.safe_load(doc_path.read_text())
    doc.pop("test_type", None)
    if test_type is not None:
        doc["test_type"] = test_type
    doc_path.write_text(yaml.safe_dump(doc, sort_keys=False))
    before = _snapshot(qdir)
    summary, raw = _trial(tmp_path, Q162)
    block = summary["holmes_compare"]
    assert block["run_mode"] == mode
    assert ("HOLMES_NO_SKILL_CATALOG" in block["disclosures"]) is flagged
    assert "KNOWLEDGE_UPSTREAM_DIRECT" in block["disclosures"]
    (call,) = [c for c in host.calls if "pytest" in c["argv"]]
    assert call["env"]["ASK_HOLMES_TEST_TYPE"] == "cli"  # never overridden
    assert "app-162" in host.ns_checks
    assert _snapshot(qdir) == before
    for needle in (*POISON, BODY_SENTINEL, GLOBAL_SENTINEL):
        assert needle not in raw


def test_c2_namespace_not_released_is_env_broken(
    tmp_path: Path, host: FakeHost, pinned: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner()
    monkeypatch.setattr(runner, "RELEASE_WAIT_S", 0)
    write_c2_question(pinned, Q94, expected=POISON[0])
    host.ns_present["app-94"] = True
    summary, _ = _trial(tmp_path, Q94)
    assert summary["status"] == "missing"
    assert "pytest" not in host.events
    assert "app-94" in host.ns_checks


# -- merge -------------------------------------------------------------------------------


def _write(root: Path, summary: dict[str, Any], block: str) -> None:
    b = summary[block]
    path = root / b["qid"] / f"trial-{b['trial']}" / "summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary))


def test_merge_c2_table(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from scripts.holmes_compare import merge

    o, h = tmp_path / "ops", tmp_path / "hol"
    for qid in C2_ALL:
        for t in (1, 2, 3):
            ops = ops_summary(qid, t, "A")
            ops["holmes_eval"]["data_plane"] = {
                "backend": "kubernetes", "mode": "none", "product_url": None,
                "port_forwards": [], "victorialogs_image": None,
            }  # fmt: skip
            _write(o, ops, "holmes_eval")
            _write(h, holmes_summary(qid, t, "B"), "holmes_compare")
    out_md, out_json = tmp_path / "t.md", tmp_path / "t.json"
    code = merge.main(
        ["--opspilot-evidence", str(o), "--holmes-evidence", str(h),
         "--out-md", str(out_md), "--out-json", str(out_json),
         "--opspilot-es-mode", "external", "--questions", "c2"]
    )  # fmt: skip
    stdout = capsys.readouterr().out
    assert code == 0, stdout
    assert "DATA_PLANE_DIFFERS" not in stdout
    result = json.loads(out_json.read_text())
    assert [r["qid"] for r in result["rows"]] == list(C2_ALL)
    assert not [r["qid"] for r in result["rows"] if r["skill_assertion_dropped"]]
    assert result["totals"]["opspilot"]["passes"] == 18
    assert result["totals"]["holmes"]["passes"] == 0
    dumped = out_json.read_text() + out_md.read_text()
    for needle in POISON:
        assert needle not in dumped
