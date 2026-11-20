"""#666 PR-F contract: the Holmes comparison side (``holmes_compare``) for
the seven C6 database questions.

Independent contract tests written from the task record's "PR-F 接口"
section ("Holmes 对照") without reading the PR-F implementation. No real
pytest harness, kubectl, database, model or judge runs: the PR-D stand-in
host (``tests/test_holmes_compare_d_contract.FakeHost``) answers
``subprocess.run`` and the namespace check. Question directories are
synthetic.

Rules under test:

* ``runner.preflight`` accepts the ``c6`` questions; they need
  ``--kubeconfig`` (``KUBECONFIG_MISSING``); no ES variables are required;
* ``pytest_env``: no ES/Loki/VictoriaLogs variables, ``OHC_SHIFT_BACKEND``
  ``none``, no OpsPilot database account (the harness uses upstream's own
  ``toolsets.yaml`` as is);
* one trial: namespace release check before and after the harness, no ES;
  the summary's ``data_plane`` (``upstream-direct``) and the disclosure
  ``DB_ADMIN_ACCOUNT_UPSTREAM``; no answer in the summary;
* ``runner round --questions c6`` selects the seven;
* ``merge --questions c6`` accepts OpsPilot rows whose ``data_plane.mode`` is
  ``port-forward`` (not ``DATA_PLANE_DIFFERS``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.holmes_evals_f_support import (
    C6_ALL,
    C6_MONGO,
    Q217,
    Q229,
    TABLE,
    port_forward,
    product_env,
    url_key,
    write_c6_question,
)
from tests.test_holmes_compare_contract import POISON, holmes_summary, ops_summary
from tests.test_holmes_compare_d_contract import FakeHost


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


def _kube(tmp_path: Path) -> Path:
    kube = tmp_path / "kubeconfig"
    kube.write_text("apiVersion: v1\n")
    return kube


# -- preflight ---------------------------------------------------------------------------


def test_preflight_accepts_c6_with_kubeconfig(tmp_path: Path, pinned: Path) -> None:
    _runner().preflight(_cfg(tmp_path, C6_ALL, kubeconfig=_kube(tmp_path)))


@pytest.mark.parametrize("qid", C6_ALL)
def test_preflight_c6_needs_kubeconfig(tmp_path: Path, pinned: Path, qid: str) -> None:
    runner = _runner()
    with pytest.raises(runner.RoundAbort) as caught:
        runner.preflight(_cfg(tmp_path, (qid,)))
    assert caught.value.code == "KUBECONFIG_MISSING"


def test_pytest_env_for_c6(tmp_path: Path) -> None:
    runner = _runner()
    kube = tmp_path / "kubeconfig"
    cfg = _cfg(tmp_path, C6_ALL, kubeconfig=kube)
    for qid in C6_ALL:
        env = runner.pytest_env(cfg, qid, tmp_path)
        assert env["OHC_QID"] == qid
        assert env["OHC_SHIFT_BACKEND"] == "none"
        assert env["KUBECONFIG"] == str(kube)
        for name in ("ELASTICSEARCH_URL", "ELASTICSEARCH_API_KEY", "OHC_LOKI_URL",
                     "VICTORIALOGS_URL"):  # fmt: skip
            assert name not in env, (qid, name)
        assert not [k for k in env if k.startswith(("OPSPILOT_", "LANGSMITH"))]
        assert "opspilot_ro" not in json.dumps(env)
        assert POISON[3] not in env.values()


def test_round_cli_selects_c6(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        "--kubeconfig", str(_kube(tmp_path)),
    ]  # fmt: skip
    assert runner.main([*base, "--questions", "c6"]) == 0
    assert tuple(seen[-1].questions) == C6_ALL
    assert runner.main([*base, "--questions", "229,230"]) == 0
    assert tuple(seen[-1].questions) == C6_MONGO


# -- one trial, stand-in harness ---------------------------------------------------------


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch, pinned: Path) -> FakeHost:
    runner = _runner()
    fake = FakeHost()
    monkeypatch.setattr(runner.subprocess, "run", fake.run)
    monkeypatch.setattr(runner, "namespace_gone", fake.namespace_gone)
    ports: list[int] = []
    monkeypatch.setattr(runner, "port_in_use", lambda port: ports.append(port) or False)
    fake.ports = ports  # type: ignore[attr-defined]

    def no_es(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("ES touched for a C6 question")

    import scripts.holmes_evals.setup as setup_mod
    import scripts.holmes_evals.shift as shift_mod

    monkeypatch.setattr(setup_mod, "purge_question_indices", no_es)
    monkeypatch.setattr(shift_mod, "EsAdmin", no_es)
    monkeypatch.setattr(runner, "start_victorialogs", no_es)
    return fake


@pytest.mark.parametrize("qid", [Q217, Q229])
def test_c6_trial_summary(
    tmp_path: Path, host: FakeHost, pinned: Path, qid: str
) -> None:
    runner = _runner()
    write_c6_question(pinned, qid, expected=POISON[0])
    cfg = _cfg(tmp_path, (qid,), kubeconfig=_kube(tmp_path))
    summary = runner.run_trial(cfg, qid, 1)
    raw = cfg.summary_path(qid, 1).read_text(encoding="utf-8")
    assert summary["status"] == "completed", summary
    block = summary["holmes_compare"]
    ns = TABLE[qid].namespace
    pytest_at = host.events.index("pytest")
    checks = [i for i, e in enumerate(host.events) if e == f"ns:{ns}"]
    assert checks and checks[0] < pytest_at < checks[-1]
    assert TABLE[qid].local_port in host.ports  # type: ignore[attr-defined]
    (call,) = [c for c in host.calls if "pytest" in c["argv"]]
    assert call["env"]["OHC_SHIFT_BACKEND"] == "none"
    plane = block["data_plane"]
    assert plane["mode"] == "upstream-direct"
    assert plane["backend"] == TABLE[qid].backend
    assert "DB_ADMIN_ACCOUNT_UPSTREAM" in block["disclosures"]
    for needle in POISON:
        assert needle not in raw


# -- merge -------------------------------------------------------------------------------


def _write(root: Path, summary: dict[str, Any], block: str) -> None:
    b = summary[block]
    path = root / b["qid"] / f"trial-{b['trial']}" / "summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary))


def test_merge_c6_table(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from scripts.holmes_compare import merge

    o, h = tmp_path / "ops", tmp_path / "hol"
    for qid in C6_ALL:
        row = TABLE[qid]
        for t in (1, 2, 3):
            ops = ops_summary(qid, t, "A")
            ops["holmes_eval"]["data_plane"] = {
                "backend": row.backend, "dialect": row.dialect, "mode": "port-forward",
                "product_url": product_env(qid, "x")[url_key(qid)],
                "port_forwards": [port_forward(qid)], "tls": row.tls,
                "victorialogs_image": None,
            }  # fmt: skip
            _write(o, ops, "holmes_eval")
            _write(h, holmes_summary(qid, t, "B"), "holmes_compare")
    out_md, out_json = tmp_path / "t.md", tmp_path / "t.json"
    code = merge.main(
        ["--opspilot-evidence", str(o), "--holmes-evidence", str(h),
         "--out-md", str(out_md), "--out-json", str(out_json),
         "--opspilot-es-mode", "external", "--questions", "c6"]
    )  # fmt: skip
    stdout = capsys.readouterr().out
    assert code == 0, stdout
    assert "DATA_PLANE_DIFFERS" not in stdout
    result = json.loads(out_json.read_text())
    assert [r["qid"] for r in result["rows"]] == list(C6_ALL)
    assert result["totals"]["opspilot"]["passes"] == 21
    assert result["totals"]["holmes"]["passes"] == 0
