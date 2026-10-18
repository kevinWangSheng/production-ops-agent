"""Implementation tests for the PR-D1 structural fixture generator and CI entry.

Offline: no model, network, PG or kind. The acceptance tests for the same
interface live in tests/acceptance/test_eval_d_*.py (independent author).
"""

from __future__ import annotations

import filecmp
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import eval_structural_ci as ci
from scripts import eval_structural_fixture as gen

ROOT = Path(__file__).resolve().parents[2]
COMMITTED = ROOT / "tests" / "fixtures" / "eval_structural"
NAME = "canary-fault-v1"
ENTRY = ROOT / "scripts" / "eval_structural_ci.py"


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.fixture
def fixtures(tmp_path: Path) -> Path:
    target = tmp_path / "fixtures"
    shutil.copytree(COMMITTED, target)
    return target


def _rewrite(path: Path, change) -> None:
    document = json.loads(path.read_bytes())
    change(document)
    path.write_text(json.dumps(document))


# --- generator ---------------------------------------------------------------


def test_generator_is_deterministic_and_matches_committed_fixture(tmp_path: Path):
    assert gen.main(["generate", "--name", NAME, "--out", str(tmp_path / "a")]) == 0
    assert gen.main(["generate", "--name", NAME, "--out", str(tmp_path / "b")]) == 0
    first = _tree(tmp_path / "a" / NAME)
    assert first == _tree(tmp_path / "b" / NAME)
    assert first == _tree(COMMITTED / NAME)
    assert not filecmp.dircmp(tmp_path / "a", tmp_path / "b").diff_files


def test_generator_refuses_existing_target_and_unknown_name(tmp_path: Path):
    (tmp_path / NAME).mkdir()
    assert gen.main(["generate", "--name", NAME, "--out", str(tmp_path)]) == 2
    assert list((tmp_path / NAME).iterdir()) == []
    assert gen.main(["generate", "--name", "canary-other", "--out", str(tmp_path)]) == 2
    assert not (tmp_path / "canary-other").exists()


def test_committed_fixture_shape():
    document = json.loads((COMMITTED / NAME / "fixture.json").read_bytes())
    assert set(document) == ci.FIXTURE_KEYS
    assert document["schema"] == "opspilot-eval-structural-fixture-1"
    assert document["attempt"]["mode"] == "structural"
    assert document["attempt"]["product_run_id"] is None
    assert document["manifest"]["dimensions"]["replay_fixture"] == {
        "kind": "hash",
        "value": document["recording_sha256"],
        "reason": None,
    }
    counts = document["expected_event_counts"]
    assert counts["model"] >= 2 and counts["tool"] >= 1 and counts["delivery"] >= 1
    assert counts["http"] >= 1
    recording = json.loads(
        (COMMITTED / NAME / "recording" / "recording.json").read_bytes()
    )
    assert recording["context"]["synthetic"] is True
    kinds = [event["kind"] for event in recording["events"]]
    assert kinds == ["model", "http", "tool", "delivery", "model", "http"]
    blob = b"\n".join(_tree(COMMITTED / NAME).values())
    assert b"http://" not in blob and b"https://" not in blob


# --- entry -------------------------------------------------------------------


def test_entry_verifies_committed_fixture():
    code, document = ci.verify(COMMITTED)
    assert code == 0
    assert document["status"] == "verified" and document["exit_code"] == 0
    assert document["results"] == [
        {
            "name": NAME,
            "recording_sha256": json.loads(
                (COMMITTED / NAME / "fixture.json").read_bytes()
            )["recording_sha256"],
            "loaded": True,
            "structural_verified": True,
            "counts_ok": True,
            "error": None,
        }
    ]


def test_flipped_blob_byte_fails(fixtures: Path):
    blob = sorted((fixtures / NAME / "recording" / "blobs").iterdir())[0]
    raw = bytearray(blob.read_bytes())
    raw[0] ^= 0x01
    blob.write_bytes(bytes(raw))
    code, document = ci.verify(fixtures)
    assert code == 1 and document["status"] == "failed"
    assert document["results"][0]["error"]


def test_tampered_recording_sha_fails(fixtures: Path):
    def change(document):
        digest = "0" * 64
        document["recording_sha256"] = digest
        document["manifest"]["dimensions"]["replay_fixture"]["value"] = digest

    _rewrite(fixtures / NAME / "fixture.json", change)
    code, document = ci.verify(fixtures)
    assert code == 1
    assert document["results"][0]["loaded"] is False
    assert document["results"][0]["error"] == "HASH_MISMATCH"


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(schema="opspilot-eval-structural-fixture-2"),
        lambda d: d.update(extra=1),
        lambda d: d.pop("attempt"),
        lambda d: d.update(name="canary-other"),
        lambda d: d["expected_event_counts"].update(model=True),
        lambda d: d["expected_event_counts"].pop("refusal"),
        lambda d: d["attempt"].update(mode="replay"),
        lambda d: d["manifest"]["dimensions"]["replay_fixture"].update(value="0" * 64),
    ],
)
def test_invalid_fixture_json_is_refused(fixtures: Path, change):
    _rewrite(fixtures / NAME / "fixture.json", change)
    code, document = ci.verify(fixtures)
    assert code == 1
    assert document["results"][0]["error"] == "FIXTURE_INVALID"


def test_count_mismatch_fails(fixtures: Path):
    _rewrite(
        fixtures / NAME / "fixture.json",
        lambda d: d["expected_event_counts"].update(http=0),
    )
    code, document = ci.verify(fixtures)
    assert code == 1
    item = document["results"][0]
    assert item["structural_verified"] is True and item["counts_ok"] is False
    assert item["error"] == "EVENT_COUNTS_MISMATCH"


def test_identity_mismatch_from_replay_is_reported(fixtures: Path):
    _rewrite(
        fixtures / NAME / "fixture.json",
        lambda d: d["attempt"].update(scenario_id="canary-elsewhere"),
    )
    code, document = ci.verify(fixtures)
    assert code == 1
    assert document["results"][0]["error"] == "IDENTITY_MISMATCH"


def test_one_failure_among_passes_fails(fixtures: Path):
    shutil.copytree(fixtures / NAME, fixtures / "canary-zz")
    code, document = ci.verify(fixtures)
    assert code == 1
    assert [item["name"] for item in document["results"]] == [NAME, "canary-zz"]
    assert document["results"][0]["error"] is None
    assert document["results"][1]["error"] == "FIXTURE_INVALID"


def test_nothing_to_verify(tmp_path: Path):
    (tmp_path / "file-only").write_text("not a fixture")
    for directory in (tmp_path, tmp_path / "missing"):
        code, document = ci.verify(directory)
        assert code == 3
        assert document["status"] == "refused" and document["results"] == []


def test_entry_process_is_offline_env_free_and_writes_nothing(fixtures: Path):
    """Socket creation disabled, environment emptied: same single JSON, no writes."""
    before = _tree(fixtures)
    probe = (
        "import runpy, sys\n"
        "def refuse(event, args):\n"
        "    if event.startswith('socket.') or event == 'subprocess.Popen':\n"
        "        raise RuntimeError('blocked: ' + event)\n"
        "sys.addaudithook(refuse)\n"
        f"sys.argv = [{str(ENTRY)!r}, '--fixtures', {str(fixtures)!r}]\n"
        f"runpy.run_path({str(ENTRY)!r}, run_name='__main__')\n"
    )
    for env in ({}, {"OPENAI_API_KEY": "CANARY-FAKE-KEY", "HOME": str(fixtures)}):
        result = subprocess.run(
            [sys.executable, "-B", "-c", probe],
            cwd=fixtures,
            env=env,
            capture_output=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        lines = result.stdout.decode().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["status"] == "verified"
    assert _tree(fixtures) == before
    assert not any(path.name == "__pycache__" for path in fixtures.rglob("*"))


def test_entry_script_exit_codes(tmp_path: Path):
    result = subprocess.run(
        [sys.executable, str(ENTRY), "--fixtures", str(tmp_path)],
        env={},
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 3
    assert json.loads(result.stdout)["exit_code"] == 3
    assert os.listdir(tmp_path) == []
