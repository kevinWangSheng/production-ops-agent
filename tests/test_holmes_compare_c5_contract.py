"""#666 PR-C contract: the Holmes comparison side (``holmes_compare``) for
the C5 questions.

Independent contract tests written from the task record's "PR-C 接口"
section ("Holmes 对照") without reading the PR-C implementation. No
upstream ``test_case.yaml`` is opened, no model, judge, Loki or
VictoriaLogs is called; ``steps shift --backend loki`` runs against an
in-process ``FakeLoki`` whose data is placed relative to the real clock so
the whole-day shift is always feasible (no fixed date, no daily window).

Rules under test:

* ``runner.preflight`` accepts every ``ALL_QUESTIONS`` id; only a selection
  with B1 questions needs ``ELASTICSEARCH_URL``/``_API_KEY``; k8s questions
  without ``--kubeconfig`` are not a preflight abort (recorded per attempt);
* the harness environment: VL questions get
  ``VICTORIALOGS_URL=http://localhost:<--victorialogs-port>`` (default
  29428); the three fixed-time Loki questions get ``OHC_SHIFT_BACKEND=loki``
  and their own ``OHC_LOKI_URL``; 282 drops the skill assertion;
* ``runner round --questions c5`` selects the nine C5 questions;
* ``steps shift --backend none`` delivers verbatim; ``--backend loki``
  applies the same whole-day shift as the driver;
* ``merge --questions c5`` builds the table; an OpsPilot C5 row whose
  ``data_plane.mode`` is ``fake`` is refused with ``DATA_PLANE_DIFFERS``.

Assumptions beyond the interface (also marked ``# ASSUMPTION:``):

* ``pytest_env`` builds ``VICTORIALOGS_URL`` and the ``OHC_SHIFT_*`` values
  (the runner hands them to pytest); its ES keys are only required for B1.
* ``OHC_LOKI_URL`` names the question's port-forward port (3110 for 100a,
  3259 for 259_loki) on localhost or 127.0.0.1.
* ``steps shift`` keeps its last-line JSON (``status``, ``applied``) and
  writes ``ShiftRecord.as_json()`` to ``--record``; ``--setup-started`` is
  ISO 8601.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.holmes_evals_b_support import no_proxy_env  # noqa: F401 - fixture
from tests.holmes_evals_c_support import (
    C5_ALL,
    Q100A,
    Q102,
    Q257,
    Q258,
    Q259L,
    Q282,
    FakeLoki,
    ns_of,
    served_loki,
)
from tests.test_holmes_compare_contract import POISON, holmes_summary, ops_summary

pytestmark = pytest.mark.usefixtures("no_proxy_env")

MONTHS = (
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
)  # fmt: skip
Q275 = "275_es_field_name_correction"


def _runner() -> Any:
    from scripts.holmes_compare import runner

    return runner


def _cfg(tmp_path: Path, questions: tuple[str, ...], **secrets: str) -> Any:
    runner = _runner()
    python = tmp_path / "holmes-python"
    python.write_text("#!/bin/sh\n")
    return runner.Config(
        round="t1",
        upstream=tmp_path / "upstream",
        holmes_python=python,
        judge_path="opspilot",
        questions=questions,
        secrets={"DEEPSEEK_API_KEY": POISON[2], "ZAI_API_KEY": POISON[3], **secrets},
    )


@pytest.fixture
def pinned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner()
    (tmp_path / "upstream").mkdir()
    monkeypatch.setattr(runner, "verify_upstream", lambda root: None)


# -- preflight --------------------------------------------------------------------------


def test_preflight_accepts_c5_without_es(tmp_path: Path, pinned: None) -> None:
    _runner().preflight(_cfg(tmp_path, C5_ALL))


def test_preflight_still_needs_es_for_b1(tmp_path: Path, pinned: None) -> None:
    runner = _runner()
    with pytest.raises(runner.RoundAbort) as caught:
        runner.preflight(_cfg(tmp_path, (Q100A, Q275)))
    assert caught.value.code == "ELASTICSEARCH_URL_MISSING"
    runner.preflight(
        _cfg(
            tmp_path,
            (Q100A, Q275),
            ELASTICSEARCH_URL="http://localhost:9200",
            ELASTICSEARCH_API_KEY=POISON[4],
        )
    )


def test_preflight_unknown_question(tmp_path: Path, pinned: None) -> None:
    runner = _runner()
    with pytest.raises(runner.RoundAbort) as caught:
        runner.preflight(_cfg(tmp_path, ("999_no_such_question",)))
    assert caught.value.code == "QUESTION_UNKNOWN"


# -- harness environment ---------------------------------------------------------------------


def test_pytest_env_for_c5_questions(tmp_path: Path) -> None:
    runner = _runner()
    cfg = _cfg(tmp_path, C5_ALL)
    for qid in C5_ALL:
        env = runner.pytest_env(cfg, qid, tmp_path)
        assert env["OHC_QID"] == qid
        assert not [k for k in env if k.startswith(("OPSPILOT_", "LANGSMITH"))]
        assert POISON[3] not in env.values()  # judge key: opspilot judge path
        assert "ELASTICSEARCH_API_KEY" not in env
    for qid in (Q257, Q258):
        env = runner.pytest_env(cfg, qid, tmp_path)
        assert env["VICTORIALOGS_URL"] == "http://localhost:29428"
    for qid in (Q100A, Q102, Q282, Q259L):
        assert "VICTORIALOGS_URL" not in runner.pytest_env(cfg, qid, tmp_path)
    for qid, port in ((Q100A, 3110), (Q259L, 3259)):
        env = runner.pytest_env(cfg, qid, tmp_path)
        assert env["OHC_SHIFT_BACKEND"] == "loki"
        assert env["OHC_LOKI_URL"].rstrip("/") in (
            f"http://localhost:{port}",
            f"http://127.0.0.1:{port}",
        )
    assert runner.pytest_env(cfg, Q282, tmp_path)["OHC_DROP_SKILL"] == "1"
    assert runner.pytest_env(cfg, Q102, tmp_path)["OHC_DROP_SKILL"] == "0"


def test_pytest_env_b1_shift_backend_is_elasticsearch(tmp_path: Path) -> None:
    runner = _runner()
    cfg = _cfg(
        tmp_path,
        (Q275,),
        ELASTICSEARCH_URL="http://localhost:9200",
        ELASTICSEARCH_API_KEY=POISON[4],
    )
    env = runner.pytest_env(cfg, Q275, tmp_path)
    assert env.get("OHC_SHIFT_BACKEND", "elasticsearch") == "elasticsearch"
    assert env["ELASTICSEARCH_URL"] == "http://localhost:9200"
    assert "VICTORIALOGS_URL" not in env


def test_round_cli_c5_and_victorialogs_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    assert runner.main([*base, "--questions", "c5"]) == 0
    assert tuple(seen[-1].questions) == C5_ALL
    env = runner.pytest_env(seen[-1], Q257, tmp_path)
    assert env["VICTORIALOGS_URL"] == "http://localhost:29428"
    assert (
        runner.main([*base, "--questions", "257", "--victorialogs-port", "29999"]) == 0
    )
    assert tuple(seen[-1].questions) == (Q257,)
    env = runner.pytest_env(seen[-1], Q257, tmp_path)
    assert env["VICTORIALOGS_URL"] == "http://localhost:29999"


# -- steps shift ------------------------------------------------------------------------------


def _steps(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
    from scripts.holmes_compare import steps

    code = steps.main(argv)
    out = capsys.readouterr().out.strip().splitlines()
    return code, json.loads(out[-1])


def test_steps_shift_backend_none_is_verbatim(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
    monkeypatch.delenv("ELASTICSEARCH_API_KEY", raising=False)
    question = "Which services have logs in the last 5 minutes?"
    qfile = tmp_path / "q.txt"
    qfile.write_text(question, encoding="utf-8")
    code, last = _steps(
        ["shift", "--backend", "none", "--qid", Q258, "--question-file", str(qfile),
         "--out", str(tmp_path / "out.txt"), "--record", str(tmp_path / "rec.json")],
        capsys,
    )  # fmt: skip
    assert code == 0 and last["status"] == "ok" and last["applied"] is False
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == question


def test_steps_shift_backend_loki_whole_days(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
    monkeypatch.delenv("ELASTICSEARCH_API_KEY", raising=False)
    now0 = datetime.now(UTC).replace(second=0, microsecond=0)
    # Incident time of day = now - 3 h: always feasible, any time of day.
    anchor = now0 - timedelta(hours=3) - timedelta(days=400)
    words = (
        f"{MONTHS[anchor.month - 1]} {anchor.day}, {anchor.year} around "
        f"{anchor:%H:%M} UTC"
    )
    question = f"The payment-api pod in namespace app-100a had issues on {words}. What happened?"
    store = FakeLoki()
    labels = {"app": "payment-api", "namespace": "app-100a"}
    store.add(
        labels,
        [
            (ns_of(anchor + timedelta(minutes=10 * i - 30)), f"refused {i}")
            for i in range(16)
        ],
    )
    store.add(dict(labels, kind="live"), [(ns_of(now0), "live")])
    qfile = tmp_path / "q.txt"
    qfile.write_text(question, encoding="utf-8")
    with served_loki(store) as url:
        code, last = _steps(
            ["shift", "--backend", "loki", "--loki-url", url,
             "--setup-started", (now0 - timedelta(seconds=10)).isoformat(),
             "--qid", Q100A, "--question-file", str(qfile),
             "--out", str(tmp_path / "out.txt"), "--record", str(tmp_path / "rec.json")],
            capsys,
        )  # fmt: skip
    assert code == 0 and last["status"] == "ok" and last["applied"] is True
    record = json.loads((tmp_path / "rec.json").read_text())
    assert record["backend"] == "loki"
    seconds = record["seconds"]
    assert seconds % 86400 == 0
    incident = anchor + timedelta(seconds=seconds)
    assert incident.date() == (now0 - timedelta(hours=3)).date()
    new_words = (
        f"{MONTHS[incident.month - 1]} {incident.day}, {incident.year} around "
        f"{incident:%H:%M} UTC"
    )
    delivered = (tmp_path / "out.txt").read_text(encoding="utf-8")
    assert delivered == question.replace(words, new_words)
    assert record["pushed"] == 16 and record["dropped_future"] == 0
    shifted = [
        ns for ns, _ in store.entries(labels) if ns > ns_of(anchor + timedelta(days=1))
    ]
    assert len(shifted) == 16


# -- merge -------------------------------------------------------------------------------------


def _ops_c5(qid: str, trial: int, mode: str) -> dict[str, Any]:
    out = ops_summary(qid, trial, "A")
    out["holmes_eval"]["data_plane"] = {
        "backend": "victorialogs" if qid in (Q257, Q258) else "loki",
        "mode": mode,
        "product_url": "http://127.0.0.1:29231",
        "gateway_upstream": None,
        "port_forwards": [],
    }
    return out


def _write(root: Path, summary: dict[str, Any], block: str) -> None:
    b = summary[block]
    path = root / b["qid"] / f"trial-{b['trial']}" / "summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary))


def _merge(tmp_path: Path, mode: str, capsys: pytest.CaptureFixture[str]):
    from scripts.holmes_compare import merge

    o, h = tmp_path / "ops", tmp_path / "hol"
    for qid in C5_ALL:
        for t in (1, 2, 3):
            _write(o, _ops_c5(qid, t, mode), "holmes_eval")
            _write(h, holmes_summary(qid, t, "B"), "holmes_compare")
    out_md, out_json = tmp_path / "t.md", tmp_path / "t.json"
    code = merge.main(
        ["--opspilot-evidence", str(o), "--holmes-evidence", str(h),
         "--out-md", str(out_md), "--out-json", str(out_json),
         "--opspilot-es-mode", "external", "--questions", "c5"]
    )  # fmt: skip
    return code, out_json, out_md, capsys.readouterr().out


@pytest.mark.parametrize("mode", ["gateway", "docker"])
def test_merge_c5_table(
    tmp_path: Path, mode: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out_json, out_md, _ = _merge(tmp_path, mode, capsys)
    assert code == 0
    result = json.loads(out_json.read_text())
    assert [r["qid"] for r in result["rows"]] == list(C5_ALL)
    flagged = {r["qid"] for r in result["rows"] if r["skill_assertion_dropped"]}
    assert flagged == {Q282}
    assert result["totals"]["opspilot"]["passes"] == 27
    assert result["totals"]["holmes"]["passes"] == 0
    dumped = out_json.read_text() + out_md.read_text()
    for needle in POISON:
        assert needle not in dumped


def test_merge_refuses_fake_c5_data_plane(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out_json, _, stdout = _merge(tmp_path, "fake", capsys)
    assert code == 2
    assert not out_json.exists()
    lines = [json.loads(x) for x in stdout.splitlines() if x.startswith("{")]
    assert lines and lines[-1]["status"] == "refused"
    assert lines[-1]["code"] == "DATA_PLANE_DIFFERS"
