"""Independent acceptance tests for #432: ``--output-root`` (seams §3, §7, D8).

Usage errors go through ``eval_live.main`` with ``--dry-run`` so no lab seam
is ever built.  The placement check builds the probe-mode ``real_deps``,
which constructs lab adapters without reading credentials or contacting
anything (the PR-C driver tests build it the same way).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import eval_live

ROOT = Path(__file__).resolve().parents[2]
FLAGD = "http://127.0.0.1:28013"


def argv(*extra: str) -> list[str]:
    return [
        "--mode",
        "live",
        "--round",
        "dv2-0a0b0c",
        "--scenario",
        "eval-payment-failure-1",
        "--n",
        "1",
        "--parallel",
        "1",
        "--holder",
        "test-432",
        "--batch",
        "b432",
        "--flagd-url",
        FLAGD,
        "--pg-port",
        "55998",
        "--dry-run",
        *extra,
    ]


def final(capsys) -> dict:
    out = capsys.readouterr().out.strip().splitlines()
    return json.loads(out[-1])


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("probe-out", id="relative"),
        pytest.param("tmp/eval-probes/432", id="relative-in-repo"),
        pytest.param(str(ROOT / "tmp" / "eval-probes-432"), id="absolute-in-repo"),
        pytest.param(str(ROOT), id="repo-root"),
        pytest.param(str(ROOT / "docs" / ".." / "tmp" / "x"), id="dotdot-into-repo"),
    ],
)
def test_relative_or_in_repo_output_root_is_a_usage_error(value, capsys):
    code = eval_live.main(argv("--output-root", value))
    assert code == 2
    document = final(capsys)
    assert document["status"] == "usage"
    assert document["exit_code"] == 2
    assert not (ROOT / "probe-out").exists()


def test_an_absolute_output_root_outside_the_repo_is_accepted(tmp_path, capsys):
    out = tmp_path / "eval-probes" / "432"
    out.mkdir(parents=True)
    code = eval_live.main(argv("--output-root", str(out)))
    assert code == 0, final(capsys)


def test_without_output_root_the_dry_run_is_unchanged(capsys):
    assert eval_live.main(argv()) == 0
    assert final(capsys)["status"] == "dry_run"


def test_output_root_moves_evidence_pg_and_attempt_ledger(tmp_path, monkeypatch):
    shared = tmp_path / "shared-ledgers"
    monkeypatch.setenv("OPSPILOT_LAB_LEDGERS", str(shared))
    out = tmp_path / "eval-probes" / "432"
    out.mkdir(parents=True)
    deps = eval_live.real_deps(FLAGD, "dv2-0a0b0c", probe=True, output_root=out)
    assert deps.evidence_root == out / "evidence"
    # §10 A1: the recording package must stay at <repo>/tmp/eval/<id>.
    assert deps.recording_root == ROOT / "tmp/eval"
    assert deps.pg_root == out / "pg"
    # D8: lab coordination files stay in the shared ledger root, so other
    # kind users' recover/preflight still see a retained intent ...
    assert deps.ledger_root == shared.resolve()
    # ... while the attempt's private ledger goes under the output root.
    private = eval_live.attempt_ledger_dir(deps, "dv2-0a0b0c", "attempt-1")
    assert private.is_relative_to(out)
    assert eval_live.recording_dir(deps, "dv2-0a0b0c", "attempt-1").is_relative_to(
        ROOT / "tmp/eval"
    )


def test_default_placement_is_unchanged(tmp_path, monkeypatch):
    shared = tmp_path / "shared-ledgers"
    monkeypatch.setenv("OPSPILOT_LAB_LEDGERS", str(shared))
    deps = eval_live.real_deps(FLAGD, "dv2-0a0b0c", probe=True)
    assert deps.evidence_root == ROOT / "docs/evidence/eval"
    assert deps.recording_root == ROOT / "tmp/eval"
    assert deps.pg_root == ROOT / "tmp/eval-live"
    assert deps.ledger_root == shared.resolve()
    private = eval_live.attempt_ledger_dir(deps, "dv2-0a0b0c", "attempt-1")
    assert private == shared.resolve() / "dv2-0a0b0c" / "attempt-1"
