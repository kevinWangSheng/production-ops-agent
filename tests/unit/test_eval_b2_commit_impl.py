"""Synthetic commit, context isolation and replay classification regressions."""

import errno
import importlib
import json
import os
from dataclasses import replace

import pytest

from opspilot.evaluation import RecordingError, load_recording
from tests.unit import test_eval_b2_impl as b2


@pytest.mark.parametrize("restore_failure", [False, True])
def test_final_marker_fsync_failure_never_publishes_a_loadable_packet(
    tmp_path, monkeypatch, restore_failure
):
    module = importlib.import_module("opspilot.evaluation.recording")
    real_publish = module._publish
    real_unlink, real_fsync = os.unlink, os.fsync
    pending = None
    failed = []

    def unlink(path, *args, **kwargs):
        nonlocal pending
        result = real_unlink(path, *args, **kwargs)
        if path == "INCOMPLETE":
            pending = kwargs["dir_fd"]
        return result

    def fsync(descriptor):
        nonlocal pending
        if descriptor == pending:
            pending = None
            failed.append(descriptor)
            raise OSError(errno.EIO, "synthetic final directory fsync failure")
        return real_fsync(descriptor)

    def publish(path, raw, media_type):
        if restore_failure and failed and path.name == "INCOMPLETE":
            raise RecordingError("EVIDENCE_UNAVAILABLE")
        return real_publish(path, raw, media_type)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", unlink)
        patch.setattr(os, "fsync", fsync)
        patch.setattr(module, "_publish", publish)
        with pytest.raises(RecordingError, match="^EVIDENCE_UNAVAILABLE$"):
            b2.source(tmp_path)
    assert len(failed) == 1
    path = tmp_path / "source" / "recording.json"
    digest = b2.sha(path.read_bytes()) if path.exists() else b2.HASH
    code = "EVIDENCE_UNAVAILABLE" if restore_failure else "RECORDING_INCOMPLETE"
    with pytest.raises(RecordingError, match=f"^{code}$"):
        load_recording(path, expected_hash=digest)


def initial_context():
    return replace(
        b2.context(),
        stimulus={"question": "synthetic only", "nested": {"value": "entry"}},
        initial_state={"business": {"value": "entry"}},
        source_versions=dict(b2.VERSIONS),
    )


def mutate_context(context, field):
    if field == "stimulus":
        context.stimulus["nested"]["value"] = "after-model"
    elif field == "initial_state":
        context.initial_state["business"]["value"] = "after-model"
    else:
        context.source_versions["code"] = "after-model"


@pytest.mark.parametrize("field", ["stimulus", "initial_state", "source_versions"])
def test_record_immutable_context_includes_all_nested_inputs(tmp_path, field):
    ctx = initial_context()

    def drive(hooks):
        hooks.model(b2.ScriptedModel([b2.reply(content="done")])).complete(b2.call())
        mutate_context(hooks.context, field)
        return replace(b2.outcome(), evidence_ids=())

    with pytest.raises(RecordingError, match="^IDENTITY_MISMATCH$"):
        b2.source(tmp_path, ctx=ctx, driver=drive)
    assert ctx == initial_context()
    path = tmp_path / "source" / "recording.json"
    assert json.loads(path.read_bytes())["complete"] is False
    assert (path.parent / "INCOMPLETE").is_file()


@pytest.mark.parametrize("field", ["stimulus", "initial_state", "source_versions"])
def test_replay_immutable_context_is_detached_and_checked(tmp_path, field):
    rec = b2.source(tmp_path, ctx=initial_context())
    original = rec.path.read_bytes()

    def drive(session):
        session.model.complete(b2.call())
        mutate_context(session.context, field)
        return replace(b2.outcome(), evidence_ids=())

    capture = b2.run_replay(
        rec, tmp_path, model=b2.ScriptedModel([b2.reply(content="done")]), driver=drive
    )
    assert capture.error == "IDENTITY_MISMATCH"
    assert capture.attempt.status == "aborted" and not capture.attempt.replay_miss
    assert rec.context == load_recording(rec.path, expected_hash=rec.ref.sha256).context
    assert rec.path.read_bytes() == original


@pytest.mark.parametrize(
    "code",
    [
        "REPLAY_MISS",
        "TIMEOUT",
        "IDENTITY_MISMATCH",
        "SCHEMA_INVALID",
        "EVIDENCE_UNAVAILABLE",
        "OUTPUT_EXISTS",
    ],
)
def test_unclassified_replay_driver_errors_cannot_choose_their_class(tmp_path, code):
    rec = b2.source(tmp_path)

    def drive(session):
        session.model.complete(b2.call())
        raise RecordingError(code)

    capture = b2.run_replay(
        rec, tmp_path, model=b2.ScriptedModel([b2.reply(content="done")]), driver=drive
    )
    assert capture.error == "DRIVER_ERROR"
    assert capture.attempt.status == "aborted" and not capture.attempt.replay_miss


def test_unclassified_replay_post_driver_failure_is_driver_error(tmp_path, monkeypatch):
    rec = b2.source(tmp_path)
    module = importlib.import_module("opspilot.evaluation.recording")
    original = module.validate_recording

    def validate(recording):
        if recording.context.source_run_id == "run-new":
            raise RecordingError("REPLAY_MISS")
        return original(recording)

    monkeypatch.setattr(module, "validate_recording", validate)
    capture = b2.run_replay(rec, tmp_path)
    assert capture.error == "DRIVER_ERROR"
    assert capture.attempt.status == "aborted" and not capture.attempt.replay_miss


def test_driver_cannot_set_the_sessions_sticky_error(tmp_path):
    rec = b2.source(tmp_path)

    def drive(session):
        session.error = "REPLAY_MISS"
        return b2.outcome()

    capture = b2.run_replay(rec, tmp_path, driver=drive)
    assert capture.error == "DRIVER_ERROR" and not capture.attempt.replay_miss


@pytest.mark.parametrize("caught", [False, True])
def test_real_capture_storage_failure_keeps_its_io_boundary(
    tmp_path, monkeypatch, caught
):
    rec = b2.source(tmp_path)
    original = os.fsync
    armed = False
    failures = []

    def fsync(descriptor):
        nonlocal armed
        if armed:
            armed = False
            failures.append(descriptor)
            raise OSError(errno.EIO, "synthetic model blob fsync failure")
        return original(descriptor)

    def drive(session):
        nonlocal armed
        armed = True
        if caught:
            with pytest.raises(RecordingError, match="^EVIDENCE_UNAVAILABLE$"):
                session.model.complete(b2.call())
            session.model.complete(b2.call())
        else:
            session.model.complete(b2.call())
        return replace(b2.outcome(), evidence_ids=())

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(RecordingError, match="^EVIDENCE_UNAVAILABLE$"):
        b2.run_replay(
            rec,
            tmp_path,
            model=b2.ScriptedModel([b2.reply(content="done")]),
            driver=drive,
        )
    assert len(failures) == 1
    path = tmp_path / "new-recording" / "recording.json"
    assert json.loads(path.read_bytes())["complete"] is False
    assert (path.parent / "INCOMPLETE").is_file()
