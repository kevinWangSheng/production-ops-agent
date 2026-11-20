"""The C4 self-built acceptance script's ``check`` verdict and manifest pin
(scripts/c4_argocd_scenario.py; C4 task record, "自建功能验收")."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import c4_argocd_scenario as scenario

GET = {
    "tool": "argocd_app_get",
    "results": [
        {"name": "guestbook", "sync": {"status": "OutOfSync", "revision": "8088f4c"}}
    ],
}
HISTORY = {
    "tool": "argocd_app_history",
    "results": [
        {"id": 0, "revision": "e27374f", "deployed_at": "2026-11-21T01:00:00Z"},
        {"id": 1, "revision": "8088f4c", "deployed_at": "2026-11-21T01:05:00Z"},
    ],
}


def _run(tmp_path: Path, summary: object, evidence: list[object]) -> int:
    (tmp_path / "summary.json").write_text(json.dumps(summary))
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    for index, item in enumerate(evidence):
        (evidence_dir / f"{index}.json").write_text(json.dumps(item))
    return scenario.main(
        [
            "check",
            "--summary",
            str(tmp_path / "summary.json"),
            "--evidence-dir",
            str(evidence_dir),
        ]
    )


def _verdict(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    verdict: dict[str, object] = json.loads(capsys.readouterr().out)
    return verdict


def test_check_passes_with_sync_status_and_two_history_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(tmp_path, {"report": "guestbook is OutOfSync"}, [GET, HISTORY]) == 0
    verdict = _verdict(capsys)
    assert verdict["ok"] is True
    assert verdict["history_rows_with_revision_and_time"] == 2


def test_check_fails_without_two_history_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    one_row = {**HISTORY, "results": HISTORY["results"][:1]}
    assert _run(tmp_path, {"report": "x"}, [GET, one_row]) == 1
    verdict = _verdict(capsys)
    assert verdict["ok"] is False
    assert verdict["history_rows_with_revision_and_time"] == 1
    no_history = tmp_path / "no-history"
    no_history.mkdir()
    assert _run(no_history, {}, [GET]) == 1


def test_check_fails_when_canary_leaks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    leaked = {**HISTORY, "values": f"c4Canary: {scenario.CANARY}"}
    assert _run(tmp_path, {"report": "x"}, [GET, leaked]) == 1
    verdict = _verdict(capsys)
    assert verdict["ok"] is False
    assert verdict["canary_leaks"] == [str(tmp_path / "evidence" / "1.json")]


def test_core_manifest_sha_mismatch_is_refused_and_images_are_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b"image: " + scenario.IMAGE_TAG.encode()
    with pytest.raises(SystemExit, match="sha256"):
        scenario.render_core_manifest(raw)
    monkeypatch.setattr(scenario, "CORE_SHA256", hashlib.sha256(raw).hexdigest())
    assert scenario.render_core_manifest(raw) == f"image: {scenario.IMAGE_PINNED}"
