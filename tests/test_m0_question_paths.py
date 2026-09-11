"""Synthetic paths must be denied before question reads or downstream work."""

import hashlib
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.m0_environment import holmes_baseline as wrapper
from scripts.m0_environment.initial_evidence import _bytes


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".ENV",
        ".env.local",
        ".EnV.TEST",
        "private-protocol/input.json",
        "PRIVATE-PROTOCOL/input.txt",
    ],
)
def test_wrapper_denies_sensitive_question_before_any_read(tmp_path, name):
    source = tmp_path / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("SYNTHETIC_NON_SECRET")
    argv = [
        "holmes",
        "--question-file",
        str(source),
        "--run-id",
        "denied",
        "--phase",
        "normal",
    ]
    with (
        patch("sys.argv", argv),
        patch.object(
            Path, "read_bytes", side_effect=AssertionError("read before denial")
        ),
        patch.object(
            wrapper, "run_child", side_effect=AssertionError("transport before denial")
        ) as transport,
    ):
        with pytest.raises(ValueError, match="INITIAL_SOURCE_PATH_DENIED"):
            wrapper.main()
        transport.assert_not_called()


@pytest.mark.parametrize(
    "kind", ["symlink", "directory", "fifo", "private-parent-link"]
)
def test_question_nonregular_or_private_alias_is_denied(tmp_path, kind):
    target = tmp_path / "question.txt"
    if kind == "symlink":
        source = tmp_path / "ordinary.txt"
        source.write_text("SYNTHETIC_NON_SECRET")
        target.symlink_to(source)
    elif kind == "directory":
        target.mkdir()
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        private = tmp_path / "private-protocol"
        private.mkdir()
        (private / "question.txt").write_text("SYNTHETIC_NON_SECRET")
        alias = tmp_path / "alias"
        alias.symlink_to(private, target_is_directory=True)
        target = alias / "question.txt"
    with patch.object(
        Path, "read_bytes", side_effect=AssertionError("denied source opened")
    ):
        with pytest.raises(ValueError, match="INITIAL_SOURCE_PATH_DENIED"):
            _bytes(target)


@pytest.mark.parametrize(
    "content",
    [
        b"Plain question\r\nwith spacing.\n",
        '{"request": "调查", "note": "unchanged"}\r\n'.encode(),
    ],
)
def test_business_question_preserves_utf8_bytes_and_hash(tmp_path, content):
    from scripts.m0_environment.initial_evidence import read_business_question

    source = tmp_path / "question.txt"
    source.write_bytes(content)
    actual = read_business_question(source).encode("utf-8")
    assert actual == content
    assert hashlib.sha256(actual).digest() == hashlib.sha256(content).digest()


@pytest.mark.parametrize("name", [".env.local", ".ENV", "PRIVATE-PROTOCOL/raw.json"])
def test_shared_evidence_reader_denies_same_paths_before_read(tmp_path, name):
    source = tmp_path / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("SYNTHETIC_NON_SECRET")
    with patch.object(
        Path, "read_bytes", side_effect=AssertionError("denied source opened")
    ):
        with pytest.raises(ValueError, match="INITIAL_SOURCE_PATH_DENIED"):
            _bytes(source)


def test_actual_legacy_wrapper_rejects_before_archive_or_dispatch(
    tmp_path, monkeypatch
):
    source = tmp_path / ".env"
    source.write_bytes(b"SYNTHETIC_NON_SECRET")
    metadata_relative = Path(
        "docs/evidence/m0-real-environment/round-02-provider-models.json"
    )
    metadata = tmp_path / metadata_relative
    metadata.parent.mkdir(parents=True)
    metadata.write_bytes((wrapper.ROOT / metadata_relative).read_bytes())
    monkeypatch.setattr(wrapper, "ROOT", tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        [
            "holmes",
            "--question-file",
            str(source),
            "--run-id",
            "denied",
            "--phase",
            "report",
            "--max-steps",
            "1",
            "--report-version",
            wrapper.LEGACY_REPORT_VERSION,
        ],
    )
    original_read = Path.read_bytes
    opened = []

    def read_spy(path):
        if path == source:
            opened.append(str(path))
            raise AssertionError("SYNTHETIC_SOURCE_OPENED_BEFORE_DENIAL")
        return original_read(path)

    with (
        patch.object(Path, "read_bytes", read_spy),
        patch.object(
            wrapper, "run_child", side_effect=AssertionError("TRANSPORT_CALLED")
        ) as transport,
    ):
        with pytest.raises(ValueError, match="INITIAL_SOURCE_PATH_DENIED"):
            wrapper.main()
        transport.assert_not_called()
    assert opened == []
    assert not (tmp_path / "tmp/m0-environment/holmes-runs/denied").exists()


def test_invalid_scope_does_not_read_an_ordinary_question(tmp_path, monkeypatch):
    source = tmp_path / "question.txt"
    source.write_text("SYNTHETIC_NON_SECRET")
    monkeypatch.setattr(
        "sys.argv",
        [
            "holmes",
            "--question-file",
            str(source),
            "--run-id",
            "bad-scope",
            "--phase",
            "normal",
            "--preflight-only",
        ],
    )
    original = Path.read_bytes
    reads = []

    def spy(path):
        if path == source:
            reads.append(path)
            raise AssertionError("question read before scope rejection")
        return original(path)

    with patch.object(Path, "read_bytes", spy):
        with pytest.raises(ValueError, match="trusted scope required"):
            wrapper.main()
    assert reads == []
