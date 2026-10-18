"""Implementer tests for ``--verify-replay`` (interface r9), offline.

A full fake round (the independent acceptance harness, used read-only as a
fixture source) produces real-format summaries and B2 recordings; the check
then runs with network and subprocess forbidden and every seam unused.
"""

from __future__ import annotations

import json
import socket
import subprocess

import pytest

from scripts import eval_live
from tests.acceptance import eval_c_support as s


@pytest.fixture
def ran_round(tmp_path, capsys):
    harness = s.Harness(tmp_path)
    code = eval_live.main(harness.argv(s.FAULT), deps=harness.deps)
    capsys.readouterr()
    assert code == 0
    return harness


@pytest.fixture
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("no network or process in --verify-replay")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(eval_live, "real_deps", forbidden)


def _verify(harness, capsys, *extra):
    code = eval_live.main(
        ["--verify-replay", "--round", s.ROUND, *extra], deps=harness.deps
    )
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_verify_replay_reloads_binds_and_replays_structurally(
    ran_round, capsys, offline
):
    calls_before = (
        len(ran_round.investigator.requests),
        len(ran_round.flags.writes),
        len(ran_round.pg.started),
    )
    code, document = _verify(ran_round, capsys)
    assert (code, document["status"]) == (0, "verified")
    (item,) = document["results"]
    assert item["loaded"] and item["binding_ok"] and item["structural_verified"]
    assert item["error"] is None and item["event_counts"]["model"] >= 1
    assert item["event_counts"]["http"] == item["event_counts"]["model"]
    stored = json.loads(
        (ran_round.deps.evidence_root / s.ROUND / "structural-replay.json").read_text()
    )
    assert stored["schema"] == "opspilot-eval-structural-replay-1"
    assert stored["results"] == document["results"]
    assert (
        len(ran_round.investigator.requests),
        len(ran_round.flags.writes),
        len(ran_round.pg.started),
    ) == calls_before
    # A second check never overwrites the first file.
    assert _verify(ran_round, capsys)[0] == 0
    assert len(list((ran_round.deps.evidence_root / s.ROUND).glob("structural-*"))) == 2


def test_verify_replay_reports_a_tampered_recording(ran_round, capsys, offline):
    path = next(ran_round.deps.recording_root.rglob("recording.json"))
    path.write_bytes(path.read_bytes() + b" ")
    code, document = _verify(ran_round, capsys)
    assert (code, document["status"]) == (1, "failed")
    (item,) = document["results"]
    assert item["error"] == "HASH_MISMATCH" and not item["structural_verified"]


def test_verify_replay_reports_a_binding_mismatch(ran_round, capsys, offline):
    summary = next(ran_round.deps.evidence_root.rglob("summary.json"))
    doc = json.loads(summary.read_text())
    doc["capture"]["usage"]["epoch"] = 999
    summary.write_text(json.dumps(doc))
    code, document = _verify(ran_round, capsys)
    assert code == 1
    (item,) = document["results"]
    assert item["loaded"] and not item["binding_ok"]
    assert item["error"] == "IDENTITY_MISMATCH"


def test_verify_replay_without_recordings_is_refused(tmp_path, capsys, offline):
    harness = s.Harness(tmp_path)
    code, document = _verify(harness, capsys)
    assert (code, document["reason"]) == (3, "NOTHING_TO_VERIFY")


def test_verify_replay_attempt_filter_and_usage(ran_round, capsys, offline):
    assert _verify(ran_round, capsys, "--attempt", "no-such-attempt")[0] == 3
    code = eval_live.main(
        ["--verify-replay", "--round", s.ROUND, "--mode", "live"], deps=ran_round.deps
    )
    assert code == 2
    capsys.readouterr()
    code = eval_live.main(
        ["--mode", "live", "--round", s.ROUND, "--attempt", "x"], deps=ran_round.deps
    )
    assert code == 2


def test_verify_replay_never_overwrites_and_retries_same_second_names(
    ran_round, capsys, offline
):
    """r10: two checks in the same second get distinct exclusive files."""
    from datetime import datetime, timezone

    from scripts.eval_live_verify import verify_replay

    deps = ran_round.deps
    now = datetime(2026, 10, 18, 1, 2, 3, tzinfo=timezone.utc)
    names = []
    for _ in range(3):
        code, document = verify_replay(
            s.ROUND,
            evidence_root=deps.evidence_root,
            recording_root=deps.recording_root,
            now=now,
        )
        assert code == 0 and "write_error" not in document
    for path in sorted((deps.evidence_root / s.ROUND).glob("structural-replay*")):
        names.append(path.name)
    assert names == [
        "structural-replay-20261018T010203Z-2.json",
        "structural-replay-20261018T010203Z.json",
        "structural-replay.json",
    ]


def test_verify_replay_reports_an_unwritable_result_with_code_5(
    ran_round, capsys, offline
):
    directory = ran_round.deps.evidence_root / s.ROUND
    directory.chmod(0o500)
    try:
        code, document = _verify(ran_round, capsys)
    finally:
        directory.chmod(0o700)
    assert code == 5
    assert document["write_error"] == "EVIDENCE_WRITE_FAILED"
    assert document["exit_code"] == 5
    assert document["results"] and document["results"][0]["structural_verified"]
