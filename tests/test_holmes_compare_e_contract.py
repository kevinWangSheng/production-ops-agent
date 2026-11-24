"""#666 PR-E contract: the Holmes comparison side (``holmes_compare``) for
question 283 and the merge's refusal of forced TodoWrite rows.

Independent contract tests written from the task record's "PR-E 接口"
section ("Holmes 对照") without reading the PR-E implementation. No real
pytest harness, kubectl, model or judge runs: the PR-D stand-in host
(``tests/test_holmes_compare_d_contract.py``) replaces ``subprocess.run`` and
the release checks. Question 283 is synthetic (its real ``before_test`` is
never read).

Rules under test: ``preflight`` takes 283 and needs ``--kubeconfig``
(``KUBECONFIG_MISSING``); ``round --questions c1`` selects 283; the runner
has no ``--todo`` and leaves upstream's own ``enable_todo`` alone (no switch
in the harness environment); the release check uses the ``app-283-``
prefix before and after the harness, and a leftover or an unanswering
kubectl keeps the harness from running; the summary's ``todo`` block;
``merge`` refuses an OpsPilot row with ``todo.source == "forced"`` and a
question whose two sides disagree on ``todo.enabled``
(``TODO_MODE_DIFFERS``), and builds the c1 table when they agree.

Assumptions beyond the interface (also marked ``# ASSUMPTION:``):

* The runner's prefix check is the module attribute
  ``runner.namespaces_with_prefix`` (as PR-D's ``runner.namespace_gone``).
* ``todo`` is a top-level key of the ``holmes_compare`` block.
* A merge refusal is exit code 2 with a last stdout line
  ``{"status": "refused", "code": ...}`` and no table (as
  ``DATA_PLANE_DIFFERS``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.holmes_evals_d_support import Q89, write_c2_question
from tests.holmes_evals_e_support import (
    NS_283,
    PREFIX,
    Q283,
    TODO_ENV,
    todo_block,
    write_283,
)
from tests.test_holmes_compare_contract import POISON, holmes_summary, ops_summary
from tests.test_holmes_compare_d_contract import (
    FakeHost,
    _cfg,
    _runner,
    _write,
    host,  # noqa: F401 - fixture
    pinned,  # noqa: F401 - fixture
)

# ruff: noqa: F811 - pytest fixtures imported from the PR-D contract module


@pytest.fixture
def prefix_host(host: FakeHost, monkeypatch: pytest.MonkeyPatch) -> FakeHost:
    runner = _runner()
    host.live: set[str] = set()  # type: ignore[misc]
    host.kubectl_down = False  # type: ignore[attr-defined]
    host.prefix_checks: list[str] = []  # type: ignore[misc]
    orig = host.run

    def run(argv, *args: Any, **kwargs: Any) -> Any:
        result = orig(argv, *args, **kwargs)
        if "pytest" in [str(a) for a in argv]:
            # Upstream's before_test/after_test run inside the harness.
            host.live = set()  # type: ignore[attr-defined]
        return result

    def namespaces_with_prefix(prefix: str, env: Any) -> list[str] | None:
        host.events.append(f"nwp:{prefix}")
        host.prefix_checks.append(prefix)  # type: ignore[attr-defined]
        if host.kubectl_down:  # type: ignore[attr-defined]
            return None
        return sorted(n for n in host.live if n.startswith(prefix))  # type: ignore[attr-defined]

    monkeypatch.setattr(runner.subprocess, "run", run)
    monkeypatch.setattr(runner, "namespaces_with_prefix", namespaces_with_prefix)
    return host


def _kube(tmp_path: Path) -> Path:
    kube = tmp_path / "kubeconfig"
    kube.write_text("apiVersion: v1\n")
    return kube


# -- preflight and command line ----------------------------------------------------------


def test_preflight_283_needs_kubeconfig(tmp_path: Path, pinned: Path) -> None:
    runner = _runner()
    with pytest.raises(runner.RoundAbort) as caught:
        runner.preflight(_cfg(tmp_path, (Q283,)))
    assert caught.value.code == "KUBECONFIG_MISSING"
    runner.preflight(_cfg(tmp_path, (Q283,), kubeconfig=_kube(tmp_path)))


def _round_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list]:
    runner = _runner()
    seen: list[Any] = []
    monkeypatch.setattr(runner, "run_round", lambda cfg: seen.append(cfg) or [])
    monkeypatch.setattr(
        runner,
        "load_env_file",
        lambda path: {"DEEPSEEK_API_KEY": POISON[2], "ZAI_API_KEY": POISON[3]},
    )
    return runner, seen


def _base(tmp_path: Path) -> list[str]:
    return [
        "round", "--round", "r1", "--upstream", str(tmp_path),
        "--holmes-python", str(tmp_path / "py"), "--env-file", str(tmp_path / "e"),
        "--kubeconfig", str(_kube(tmp_path)),
    ]  # fmt: skip


def test_round_cli_selects_c1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, seen = _round_main(tmp_path, monkeypatch)
    assert runner.main([*_base(tmp_path), "--questions", "c1"]) == 0
    assert tuple(seen[-1].questions) == (Q283,)
    assert runner.main([*_base(tmp_path), "--questions", "283"]) == 0
    assert tuple(seen[-1].questions) == (Q283,)


@pytest.mark.parametrize("mode", ["on", "off", "question"])
def test_runner_has_no_todo_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    runner, seen = _round_main(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as caught:
        runner.main([*_base(tmp_path), "--questions", "c1", "--todo", mode])
    assert caught.value.code == 2
    assert seen == []


def test_pytest_env_for_283_has_no_switch(tmp_path: Path) -> None:
    runner = _runner()
    kube = _kube(tmp_path)
    env = runner.pytest_env(_cfg(tmp_path, (Q283,), kubeconfig=kube), Q283, tmp_path)
    assert env["OHC_QID"] == Q283
    assert env["KUBECONFIG"] == str(kube)
    assert TODO_ENV not in env
    assert not [k for k in env if "TODO" in k.upper()]
    assert not [k for k in env if k.startswith(("OPSPILOT_", "LANGSMITH"))]
    for name in ("ELASTICSEARCH_URL", "ELASTICSEARCH_API_KEY", "OHC_LOKI_URL",
                 "VICTORIALOGS_URL"):  # fmt: skip
        assert name not in env, name


# -- one trial ---------------------------------------------------------------------------


def _trial(tmp_path: Path, qid: str) -> tuple[dict, str]:
    runner = _runner()
    cfg = _cfg(tmp_path, (qid,), kubeconfig=_kube(tmp_path))
    out = runner.run_trial(cfg, qid, 1)
    raw = cfg.summary_path(qid, 1).read_text(encoding="utf-8")
    return out, raw


def test_283_trial_summary_and_prefix_checks(
    tmp_path: Path, prefix_host: FakeHost, pinned: Path
) -> None:
    qdir = write_283(pinned, expected=POISON[0])
    before = (qdir / "test_case.yaml").read_bytes()
    summary, raw = _trial(tmp_path, Q283)
    assert summary["status"] == "completed", summary
    block = summary["holmes_compare"]
    assert block["todo"] == {"mode": "question", "enabled": True, "source": "question"}
    at = prefix_host.events.index("pytest")
    checks = [i for i, e in enumerate(prefix_host.events) if e == f"nwp:{PREFIX}"]
    assert checks and checks[0] < at < checks[-1]
    assert set(prefix_host.prefix_checks) == {PREFIX}
    (call,) = [c for c in prefix_host.calls if "pytest" in c["argv"]]
    assert "--todo" not in call["argv"]
    assert TODO_ENV not in call["env"]
    assert (qdir / "test_case.yaml").read_bytes() == before  # upstream as is
    for needle in POISON:
        assert needle not in raw


def test_question_without_enable_todo_records_off(
    tmp_path: Path, prefix_host: FakeHost, pinned: Path
) -> None:
    write_c2_question(pinned, Q89, expected=POISON[0])
    summary, _ = _trial(tmp_path, Q89)
    assert summary["status"] == "completed", summary
    todo = summary["holmes_compare"]["todo"]
    assert todo == {"mode": "question", "enabled": False, "source": "question"}
    assert prefix_host.prefix_checks == []


@pytest.mark.parametrize("why", ["leftover", "kubectl_down"])
def test_283_not_released_keeps_harness_from_running(
    tmp_path: Path,
    prefix_host: FakeHost,
    pinned: Path,
    monkeypatch: pytest.MonkeyPatch,
    why: str,
) -> None:
    runner = _runner()
    monkeypatch.setattr(runner, "RELEASE_WAIT_S", 0)
    write_283(pinned, expected=POISON[0])
    if why == "leftover":
        prefix_host.live = {NS_283[0]}  # type: ignore[attr-defined]
    else:
        prefix_host.kubectl_down = True  # type: ignore[attr-defined]
    summary, _ = _trial(tmp_path, Q283)
    assert summary["status"] == "missing"
    assert "pytest" not in prefix_host.events
    assert PREFIX in prefix_host.prefix_checks  # type: ignore[attr-defined]


# -- merge -------------------------------------------------------------------------------

PLANE = {
    "backend": "kubernetes", "mode": "none", "product_url": None,
    "port_forwards": [], "victorialogs_image": None,
}  # fmt: skip


def _merge(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    ops_enabled: bool,
    ops_source: str,
    holmes_enabled: bool,
) -> tuple[int, Path, str]:
    from scripts.holmes_compare import merge

    o, h = tmp_path / "ops", tmp_path / "hol"
    mode = "question" if ops_source == "question" else ("on" if ops_enabled else "off")
    for t in (1, 2, 3):
        ops = ops_summary(Q283, t, "A")
        ops["holmes_eval"]["data_plane"] = dict(PLANE)
        ops["holmes_eval"]["todo"] = todo_block(ops_enabled, ops_source, mode)
        _write(o, ops, "holmes_eval")
        hol = holmes_summary(Q283, t, "B")
        hol["holmes_compare"]["todo"] = {
            "mode": "question", "enabled": holmes_enabled, "source": "question",
        }  # fmt: skip
        _write(h, hol, "holmes_compare")
    out_md, out_json = tmp_path / "t.md", tmp_path / "t.json"
    code = merge.main(
        ["--opspilot-evidence", str(o), "--holmes-evidence", str(h),
         "--out-md", str(out_md), "--out-json", str(out_json),
         "--opspilot-es-mode", "external", "--questions", "c1"]
    )  # fmt: skip
    return code, out_json, capsys.readouterr().out


def test_merge_c1_table_when_both_follow_the_question(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out_json, stdout = _merge(
        tmp_path, capsys, ops_enabled=True, ops_source="question", holmes_enabled=True
    )
    assert code == 0, stdout
    result = json.loads(out_json.read_text())
    assert [r["qid"] for r in result["rows"]] == [Q283]
    assert result["totals"]["opspilot"]["passes"] == 3
    assert result["totals"]["holmes"]["passes"] == 0
    assert "TODO_MODE_DIFFERS" not in stdout
    for needle in POISON:
        assert needle not in out_json.read_text()


@pytest.mark.parametrize(
    ("ops_enabled", "ops_source", "holmes_enabled"),
    [
        (True, "forced", True),  # forced on, same value: still refused
        (False, "forced", True),  # forced off
        (False, "question", True),  # sides disagree
        (True, "question", False),
    ],
)
def test_merge_refuses_forced_or_differing_todo(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ops_enabled: bool,
    ops_source: str,
    holmes_enabled: bool,
) -> None:
    code, out_json, stdout = _merge(
        tmp_path, capsys, ops_enabled=ops_enabled, ops_source=ops_source,
        holmes_enabled=holmes_enabled,
    )  # fmt: skip
    assert code == 2, stdout
    assert not out_json.exists()
    lines = [json.loads(x) for x in stdout.splitlines() if x.startswith("{")]
    assert lines and lines[-1]["status"] == "refused"
    assert lines[-1]["code"] == "TODO_MODE_DIFFERS"
