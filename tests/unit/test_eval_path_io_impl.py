"""Local IO failures are fixed codes, never raw path text or leaked dir fds."""

import errno
import importlib
import os
from dataclasses import replace

import pytest

from opspilot.evaluation import (
    AttemptJournal,
    StorageError,
    load_attempt,
    load_recording,
    record,
    replay,
    write_attempt,
)
from tests.unit import test_eval_b1_impl as b1
from tests.unit import test_eval_b2_impl as b2


@pytest.mark.parametrize("boundary", ["record", "load", "load_incomplete"])
def test_recording_final_directory_close_failure_is_normalized(
    tmp_path, monkeypatch, boundary
):
    recording_module = importlib.import_module("opspilot.evaluation.recording")
    source = b2.source(tmp_path) if boundary != "record" else None
    if boundary == "load_incomplete":
        (source.path.parent / "INCOMPLETE").write_bytes(b"RECORDING_INCOMPLETE")
    target = source.path if source else tmp_path / "source" / "INCOMPLETE"
    real_parent, real_close = recording_module._parent, os.close
    pending = None
    failed = []

    def tracked_parent(path):
        nonlocal pending
        parent, name = real_parent(path)
        if path == target:
            pending = parent
        return parent, name

    def failed_close(descriptor):
        nonlocal pending
        real_close(descriptor)
        if descriptor == pending:
            pending = None
            failed.append(descriptor)
            raise OSError(
                errno.EIO, "synthetic close failure", "synthetic-private-path"
            )

    with monkeypatch.context() as patch:
        patch.setattr(recording_module, "_parent", tracked_parent)
        patch.setattr(os, "close", failed_close)
        with pytest.raises(StorageError, match="^EVIDENCE_UNAVAILABLE$") as caught:
            if source:
                load_recording(source.path, expected_hash=source.ref.sha256)
            else:
                b2.source(tmp_path)
    assert len(failed) == 1
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    with pytest.raises(OSError) as closed:
        os.fstat(failed[0])
    assert closed.value.errno == errno.EBADF


@pytest.mark.parametrize("bad", ["\x00", "\ud800"], ids=["nul", "surrogate"])
@pytest.mark.parametrize("position", ["leaf", "parent"])
@pytest.mark.parametrize(
    "boundary",
    [
        "attempt_read",
        "attempt_write",
        "recording_read",
        "recording_write",
        "recording_live_path",
        "replay_read",
        "replay_write",
    ],
)
def test_invalid_local_paths_have_fixed_errors_and_no_directory_fd_leak(
    tmp_path, monkeypatch, bad, position, boundary
):
    name = "synthetic-private-path-" + bad
    path = tmp_path / name if position == "leaf" else tmp_path / name / "file.json"
    cap = b1.capture() if boundary == "attempt_write" else None
    if boundary.startswith("replay"):
        rec = b2.source(tmp_path)
        mode = "structural" if boundary == "replay_read" else "replay"
        manifest, attempt = b2.manifest_attempt(rec, mode)

    def never_called(*args):
        pytest.fail("invalid local path must fail before driver/model invocation")

    def action():
        if boundary == "attempt_read":
            return load_attempt(path)
        if boundary == "attempt_write":
            return write_attempt(
                path, AttemptJournal(cap.attempt, cap.spec, cap.answer)
            )
        if boundary == "recording_read":
            return load_recording(path, expected_hash=b2.HASH)
        if boundary in ("recording_write", "recording_live_path"):
            ctx = replace(b2.context(), synthetic=boundary != "recording_live_path")
            return record(never_called, destination=path, context=ctx)
        if boundary == "replay_read":
            return replay(
                replace(rec, path=path),
                mode="structural",
                manifest=manifest,
                attempt=attempt,
                investigator=None,
            )
        return replay(
            rec,
            mode="replay",
            manifest=manifest,
            attempt=attempt,
            investigator=type("UnusedModel", (), {"complete": never_called})(),
            driver=never_called,
            destination=path,
        )

    real_open, real_close = os.open, os.close
    directories = set()

    def tracked_open(name, flags, *args, **kwargs):
        fd = real_open(name, flags, *args, **kwargs)
        if flags & os.O_DIRECTORY:
            directories.add(fd)
        return fd

    def is_open(fd):
        try:
            os.fstat(fd)
        except OSError as exc:
            assert exc.errno == errno.EBADF
            return False
        return True

    error = None
    with monkeypatch.context() as patch:
        patch.setattr(os, "open", tracked_open)
        try:
            action()
        except Exception as exc:
            error = exc
        finally:
            leaked = {fd for fd in directories if is_open(fd)}
            # A red run must not contaminate other tests with its leaked fds.
            for fd in leaked:
                real_close(fd)
    assert not leaked, "directory descriptor leaked on path-conversion failure"
    assert isinstance(error, StorageError), type(error)
    # B1 I:93 keeps invalid returned ref names as schema errors. Ancestor
    # conversion errors and readers/directory writers are IO failures (I:95).
    code = (
        "SCHEMA_INVALID"
        if boundary == "attempt_write" and position == "leaf"
        else "EVIDENCE_UNAVAILABLE"
    )
    assert error.code == code
    assert str(error) == code
    assert error.__cause__ is None and error.__context__ is None
    assert not hasattr(error, "filename")
    assert not list(tmp_path.glob(".eval-*.tmp"))


def test_parent_walk_closes_child_when_previous_directory_close_fails(
    tmp_path, monkeypatch
):
    real_open, real_close = os.open, os.close
    opened = set()
    fail_once = True

    def tracked_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.add(fd)
        return fd

    def interrupted_close(fd):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise OSError(
                errno.EIO, "synthetic close failure", "synthetic-private-path"
            )
        return real_close(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", tracked_open)
        patch.setattr(os, "close", interrupted_close)
        with pytest.raises(StorageError, match="^EVIDENCE_UNAVAILABLE$") as caught:
            load_attempt(tmp_path / "file.json")
    leaked = []
    for fd in opened:
        try:
            os.fstat(fd)
        except OSError as exc:
            assert exc.errno == errno.EBADF
        else:
            leaked.append(fd)
            real_close(fd)
    assert not leaked
    assert caught.value.__cause__ is None and caught.value.__context__ is None
