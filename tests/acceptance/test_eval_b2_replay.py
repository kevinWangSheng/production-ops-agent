"""Independent acceptance tests for eval suite PR-B2 (recording and replay).

Sources: docs/tasks/2026-10-14-eval-pr-b2-interface.md ("I:<line>") and the
PR-B pre-review docs/tasks/2026-10-14-eval-pr-b-contract.md ("C:<line>"),
rulings B1 = B / B2 = B (C:4). Author did not read the B2 implementation.
All data is synthetic and on tmp_path; no model, network, PG or kind.
Ambiguous points and the reading adopted: EVAL_B2_AMBIGUITIES.md.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from http.client import IncompleteRead
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.error import HTTPError, URLError

import pytest

from opspilot.evaluation import (
    ArtifactRef,
    DimensionValue,
    RecordingError,
    StorageError,
    canonical_view_hash,
    load_recording,
    record,
    replay,
)
from opspilot.evaluation.codec import decode
from opspilot.investigation.client import DeepSeekClient
from opspilot.investigation.context import messages_hash
from opspilot.investigation.loop import (
    ModelCall,
    ModelError,
    ModelReply,
    serialized_request,
)
from opspilot.investigation.store import MemoryStepStore
from tests.acceptance import eval_b2_support as s
from tests.m1_investigation_support import (
    ScriptedModel,
    assemble,
    report_from_transcript,
)
from tests.m1_investigation_support import reply as loop_reply
from tests.m1_investigation_support import tool_call as loop_tool_call
from tests.m1_tool_support import build as build_tool_executor
from tests.m1_tool_support import registration as tool_registration
from tests.m1_tool_support import request as tool_request_fixture

MiB = 1024 * 1024


# --- helpers -------------------------------------------------------------------


def make(tmp_path: Path, driver: Any = None, *, name: str = "pkg", **ctx: Any):
    pkg = tmp_path / name
    ref = record(
        s.source_driver() if driver is None else driver,
        destination=pkg,
        context=s.context(**ctx),
    )
    return pkg, ref


def record_code(tmp_path: Path, driver: Any, *, name: str = "pkg", **ctx: Any):
    try:
        make(tmp_path, driver, name=name, **ctx)
    except RecordingError as exc:
        assert str(exc) == exc.code
        return exc.code
    return None


def assert_not_complete(pkg: Path) -> None:
    """I:41: a failed recording leaves complete=false (or no recording.json)."""
    path = pkg / "recording.json"
    if path.exists():
        doc = s.package_json(pkg)
        assert doc["complete"] is False
        assert doc["incomplete_reasons"]


INCOMPLETE_CODES = {"RECORDING_INCOMPLETE"}


def loaded(pkg: Path, ref: ArtifactRef):
    return load_recording(pkg / ref.path, expected_hash=ref.sha256)


class Investigator(s.FakeModel):
    def __init__(
        self, tool_calls: int = 1, *, reasoning: str = "NEW-REASONING-1"
    ) -> None:
        super().__init__(
            [
                s.reply(
                    calls=tuple(f"new_call_{i}" for i in range(tool_calls)),
                    reasoning=reasoning,
                    response_id="new-1",
                ),
                s.reply(content='{"summary":"done"}', response_id="new-2"),
            ]
        )


def replay_driver(
    requests: list[dict[str, Any]],
    log: dict[str, Any],
    *,
    echo_reasoning: Any = "own",
    swallow: bool = False,
    mutate_first: Any = None,
):
    def driver(session: Any):
        log["now"] = session.clock.now()
        log["mono"] = session.clock.monotonic()
        log["context"] = session.context
        seen: list[Any] = []
        log["seen"] = seen
        model = session.model
        if mutate_first is not None:
            model = mutate_first(session.model)
        try:
            s.investigate(
                model=model,
                tools=session.tools,
                deliver=session.capture_delivery,
                requests=requests,
                step_id="new-step-1",
                echo_reasoning=echo_reasoning,
                final_timeout=1000.0,
                seen=seen,
            )
        except Exception:
            if not swallow:
                raise
            # A misbehaving driver keeps going after the miss (I:71 "停止该 Run").
            try:
                session.model.complete(
                    ModelCall(
                        messages=({"role": "user", "content": "again"},),
                        tools=None,
                        json_mode=False,
                        max_tokens=10,
                        timeout_seconds=10.0,
                    )
                )
            except Exception:
                pass
            raise
        ids = tuple(o.evidence.evidence_id for o in seen if o.evidence is not None)
        return s.outcome(ids)

    return driver


def run_replay(
    tmp_path: Path,
    rec: Any,
    requests: list[dict[str, Any]],
    *,
    investigator: Any = None,
    frozen: Any = None,
    log: dict[str, Any] | None = None,
    **driver_kw: Any,
):
    log = {} if log is None else log
    investigator = Investigator(len(requests)) if investigator is None else investigator
    frozen = s.manifest() if frozen is None else frozen
    capture = replay(
        rec,
        mode="replay",
        attempt=s.attempt("replay", frozen),
        manifest=frozen,
        investigator=investigator,
        driver=replay_driver(requests, log, **driver_kw),
        destination=tmp_path / "replayed",
    )
    return capture, investigator, log


def assert_miss(capture: Any) -> None:
    # I:44 / I:90: miss -> status=aborted, replay_miss=True (PR-A status enum).
    assert capture.attempt.replay_miss is True
    assert capture.attempt.status == "aborted"


def assert_refused(fn: Any) -> None:
    """I:58/I:92 refusal: either a fixed-code error or a failed capture."""
    try:
        capture = fn()
    except RecordingError as exc:
        assert str(exc) == exc.code
        return
    assert capture.error is not None


@pytest.fixture
def source(tmp_path: Path):
    pkg, ref = make(tmp_path, name="source")
    return pkg, ref, loaded(pkg, ref)


# --- error type ------------------------------------------------------------------


def test_recording_error_is_b1_storage_error():
    # I:75: RecordingError reuses B1 StorageError (alias).
    assert RecordingError is StorageError


# --- recording format ------------------------------------------------------------


def test_record_writes_versioned_canonical_complete_package(tmp_path):
    pkg, ref = make(tmp_path)
    data = (pkg / "recording.json").read_bytes()
    # I:41: ref.path is recording.json; I:69 refs keep all 5 B1 fields, private.
    assert ref.path == "recording.json"
    assert ref.sha256 == s.sha(data) and ref.bytes == len(data)
    assert ref.visibility == "private" and ref.media_type
    # I:62 + I:69: canonical JSON, no newline, all top-level fields required.
    doc = json.loads(data)
    assert data == s.canonical_bytes(doc)
    assert set(doc) == {
        "schema",
        "context",
        "events",
        "outcome",
        "complete",
        "incomplete_reasons",
    }
    assert doc["schema"] == "opspilot-replay-1"
    assert doc["complete"] is True and doc["incomplete_reasons"] == []
    # I:94: explicit input_refs/source_versions/clock_policy, defaults included.
    assert {"input_refs", "source_versions", "clock_policy"} <= set(doc["context"])
    assert doc["context"]["clock_policy"] == "event-clock/frozen-end-real-monotonic"
    rec = loaded(pkg, ref)
    assert rec.complete is True and rec.incomplete_reasons == ()
    assert rec.ref == ref
    assert rec.context.source_run_id == s.SOURCE_RUN
    assert rec.outcome == s.outcome(("ev-1",))


def test_events_are_ordered_with_run_identity_monotonic_clock_and_parents(tmp_path):
    pkg, _ = make(tmp_path)
    events = s.package_json(pkg)["events"]
    # I:62: seq from 0 contiguous, identity=source_run_id, clock monotonic
    # within [started_at, ended_at], call-start order.
    assert [e["seq"] for e in events] == list(range(len(events)))
    assert [e["kind"] for e in events] == ["model", "tool", "delivery", "model"]
    assert {e["identity"] for e in events} == {s.SOURCE_RUN}
    clocks = [e["clock"] for e in events]
    parsed = [datetime.fromisoformat(c.replace("Z", "+00:00")) for c in clocks]
    assert parsed == sorted(parsed)
    assert s.START <= parsed[0] and parsed[-1] <= s.END
    # I:62: delivery parent points to the tool event that produced it.
    assert events[2]["parent"] == events[1]["seq"]


def test_every_artifact_ref_is_private_content_addressed_regular_blob(tmp_path):
    pkg, _ = make(tmp_path)
    doc = s.package_json(pkg)
    refs = list(doc["context"]["input_refs"])
    for event in doc["events"]:
        refs.append(event["request_ref"])
        if event["response_ref"] is not None:
            refs.append(event["response_ref"])
    for ref in refs:
        # I:69: B1's 5 fields, visibility=private, raw bytes at blobs/<sha256>.
        assert set(ref) == {"path", "sha256", "bytes", "media_type", "visibility"}
        assert ref["visibility"] == "private"
        assert ref["path"] == f"blobs/{ref['sha256']}"
        path = pkg / ref["path"]
        assert path.is_file() and not path.is_symlink()
        data = path.read_bytes()
        assert len(data) == ref["bytes"] and s.sha(data) == ref["sha256"]


def test_identical_content_is_stored_once(tmp_path):
    view = {"tool": s.TOOL, "status": "ok", "content": [{"same": True}]}
    outcomes = [
        (lambda req: s.tool_outcome(req, evidence_id="ev-1", view=view)),
        (lambda req: s.tool_outcome(req, evidence_id="ev-1", view=view)),
    ]
    pkg, _ = make(tmp_path, s.source_driver([s.REQ_A, s.REQ_B], outcomes=outcomes))
    deliveries = s.events_of(s.package_json(pkg), "delivery")
    # I:69: same content deduplicated -> both full-view refs share one blob.
    assert deliveries[0]["request_ref"] == deliveries[1]["request_ref"]
    blobs = [p.read_bytes() for p in (pkg / "blobs").iterdir()]
    assert len(blobs) == len(set(blobs))


def test_package_has_private_permissions(tmp_path):
    pkg, _ = make(tmp_path)
    # I:41: default private permissions.
    assert pkg.stat().st_mode & 0o077 == 0
    assert (pkg / "recording.json").stat().st_mode & 0o077 == 0


def test_model_event_names_body_and_messages_hashes_separately(tmp_path):
    log: dict[str, Any] = {}
    pkg, _ = make(tmp_path, s.source_driver(log=log))
    first = log["first"]
    event = s.events_of(s.package_json(pkg), "model")[0]
    # I:64: hashes={request_body_sha256,messages_sha256}; body over
    # serialized_request bytes, messages over canonical messages.
    assert event["hashes"] == {
        "request_body_sha256": s.sha(serialized_request(first)),
        "messages_sha256": messages_hash(first.messages),
    }
    request = s.blob_json(pkg, event["request_ref"])
    assert set(request) == {"call", "body"}
    # I:69: the original body keeps product spacing and key order.
    assert request["body"] == serialized_request(first).decode("utf-8")
    assert set(request["call"]) == {f.name for f in dataclasses.fields(ModelCall)}
    response = s.blob_json(pkg, event["response_ref"])
    assert set(response) == {f.name for f in dataclasses.fields(ModelReply)}
    assert [c["id"] for c in response["tool_calls"]] == ["src_call_0"]


def test_timeout_is_not_mixed_into_request_body_hash(tmp_path):
    def driver(timeout: float):
        def run(hooks: Any):
            model = hooks.model(
                s.FakeModel([s.reply(content="a"), s.reply(content="b")])
            )
            call = ModelCall(
                messages=({"role": "user", "content": "q"},),
                tools=None,
                json_mode=False,
                max_tokens=10,
                timeout_seconds=timeout,
            )
            hooks.clock(s.START + timedelta(minutes=1))
            model.complete(call)
            return s.outcome(())

        return run

    a, _ = make(tmp_path, driver(360.0), name="a")
    b, _ = make(tmp_path, driver(17.0), name="b")
    ea = s.events_of(s.package_json(a), "model")[0]
    eb = s.events_of(s.package_json(b), "model")[0]
    # I:48: timeout stays out of the body hash; the call field still records it.
    assert ea["hashes"]["request_body_sha256"] == eb["hashes"]["request_body_sha256"]
    assert s.blob_json(a, ea["request_ref"])["call"]["timeout_seconds"] == 360.0
    assert s.blob_json(b, eb["request_ref"])["call"]["timeout_seconds"] == 17.0


def test_tool_event_records_request_binding_key_occurrence_and_hashes(tmp_path):
    raw = b'{"spans":[{"service":"cart","error":true}]}'
    log: dict[str, Any] = {}
    pkg, _ = make(tmp_path, s.source_driver([s.REQ_A, s.REQ_A], log=log))
    tools = s.events_of(s.package_json(pkg), "tool")
    assert len(tools) == 2
    expected_key = s.sha(
        s.canonical_bytes(
            {
                "fixture_hash": s.context().source_manifest_hash,
                "binding": asdict(s.binding()),
                "tool_name": s.REQ_A["tool_name"],
                "target_ref": s.REQ_A["target_ref"],
                "params": s.REQ_A["params"],
                "window": s.REQ_A["window"],
            }
        )
    )
    for occurrence, (event, result) in enumerate(zip(tools, log["seen"], strict=True)):
        request = s.blob_json(pkg, event["request_ref"])
        # I:66: {request, binding, key, occurrence}; I:71: key formula with
        # fixture_hash=source_manifest_hash during recording; occurrence from 0.
        assert set(request) == {"request", "binding", "key", "occurrence"}
        assert request["key"] == expected_key
        assert request["occurrence"] == occurrence
        assert request["binding"] == asdict(s.binding())
        assert request["request"]["params"] == s.REQ_A["params"]
        # I:66: hashes={view_sha256,raw_sha256}; raw is lowercase hex in the outcome.
        assert event["hashes"] == {
            "view_sha256": canonical_view_hash(result.model_view),
            "raw_sha256": s.sha(raw),
        }
        response = s.blob_json(pkg, event["response_ref"])
        assert response["evidence"]["raw"] == raw.hex()


def test_tool_without_evidence_hashes_empty_raw(tmp_path):
    outcomes = [lambda req: s.tool_outcome(req, evidence_id="none", raw=None)]
    pkg, _ = make(tmp_path, s.source_driver(outcomes=outcomes, result=s.outcome(())))
    event = s.events_of(s.package_json(pkg), "tool")[0]
    # I:66: no evidence.raw -> raw hash is SHA256(b'').
    assert event["hashes"]["raw_sha256"] == s.sha(b"")


def test_failed_physical_model_call_is_kept_as_its_own_event(tmp_path):
    def driver(hooks: Any):
        fake = s.FakeModel([ModelError("MODEL_UNAVAILABLE"), s.reply(content="ok")])
        model = hooks.model(fake)
        call = ModelCall(
            messages=({"role": "user", "content": "q"},),
            tools=None,
            json_mode=False,
            max_tokens=10,
            timeout_seconds=30.0,
        )
        hooks.clock(s.START + timedelta(minutes=1))
        try:
            model.complete(call)
        except ModelError:
            pass
        model.complete(call)  # the loop's retry
        return s.outcome(())

    pkg, _ = make(tmp_path, driver)
    doc = s.package_json(pkg)
    models = s.events_of(doc, "model")
    # I:48/I:62/C:117: each physical request including failures and retries.
    assert doc["complete"] is True
    assert len(models) == 2
    assert models[0]["error"] is not None and models[0]["response_ref"] is None
    assert models[1]["error"] is None and models[1]["response_ref"] is not None


def test_synthetic_without_input_freezes_stimulus_canonical_json(tmp_path):
    pkg, ref = make(tmp_path)
    rec = loaded(pkg, ref)
    # I:94: synthetic without input auto-freezes stimulus canonical JSON.
    assert len(rec.context.input_refs) == 1
    blob = (pkg / rec.context.input_refs[0].path).read_bytes()
    assert blob == s.canonical_bytes(dict(s.context().stimulus))


def test_input_hook_keeps_original_raw_bytes(tmp_path):
    raw = b'{ "question" : "why",  "target_id": "target-demo" }'
    saved: dict[str, Any] = {}

    def driver(hooks: Any):
        saved["ref"] = hooks.input(raw)
        return s.source_driver()(hooks)

    pkg, ref = make(tmp_path, driver)
    rec = loaded(pkg, ref)
    # I:94: hooks.input saves the original JSON bytes and returns its ref.
    assert isinstance(saved["ref"], ArtifactRef)
    assert saved["ref"] in rec.context.input_refs
    assert (pkg / saved["ref"].path).read_bytes() == raw


def test_existing_destination_is_never_overwritten(tmp_path):
    existing = tmp_path / "pkg"
    existing.mkdir()
    (existing / "keep.txt").write_bytes(b"user data")
    # I:41 / I:84: exclusive creation, OUTPUT_EXISTS.
    assert record_code(tmp_path, s.source_driver()) == "OUTPUT_EXISTS"
    assert (existing / "keep.txt").read_bytes() == b"user data"
    assert sorted(p.name for p in existing.iterdir()) == ["keep.txt"]


def test_symlink_destination_is_output_exists(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "pkg").symlink_to(real, target_is_directory=True)
    # I:84: including a symbolic link.
    assert record_code(tmp_path, s.source_driver()) == "OUTPUT_EXISTS"
    assert list(real.iterdir()) == []


def test_non_synthetic_package_outside_repo_tmp_eval_is_refused(tmp_path):
    # I:41: non-synthetic packages must live under the repo's ignored tmp/eval/.
    with pytest.raises(RecordingError):
        make(tmp_path, synthetic=False)
    assert not (tmp_path / "pkg" / "recording.json").exists() or (
        s.package_json(tmp_path / "pkg")["complete"] is False
    )


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"source_manifest_hash": "A" * 64}, id="uppercase-hash"),
        pytest.param({"source_manifest_hash": "abc"}, id="short-hash"),
        pytest.param({"stimulus": {"question": float("nan")}}, id="non-finite"),
        pytest.param({"bindings": (s.binding(), s.binding())}, id="duplicate-binding"),
    ],
)
def test_invalid_context_is_schema_invalid(tmp_path, changes):
    # I:8 strict JSON + 64 lowercase hex; I:83 illegal hash / duplicate binding.
    with pytest.raises(RecordingError) as err:
        make(tmp_path, **changes)
    assert err.value.code == "SCHEMA_INVALID"


def test_unbound_tool_target_is_refused_not_guessed(tmp_path):
    other = dict(s.REQ_A, target_ref="target-unbound")
    # I:50/I:92: bindings looked up exactly; unfrozen tool/target refuses the fixture.
    with pytest.raises(RecordingError):
        make(tmp_path, s.source_driver([other]))
    assert_not_complete(tmp_path / "pkg")


# --- incomplete / size limits / clock / control ------------------------------------


def test_oversize_tool_raw_is_incomplete_without_truncated_data(tmp_path):
    big = b"Z" * (2 * MiB + 1)
    outcomes = [lambda req: s.tool_outcome(req, evidence_id="ev-1", raw=big)]
    # I:69: tool raw 2 MiB; oversize writes no truncated data, lands incomplete.
    assert record_code(tmp_path, s.source_driver(outcomes=outcomes)) == (
        "RECORDING_INCOMPLETE"
    )
    pkg = tmp_path / "pkg"
    assert_not_complete(pkg)
    if (pkg / "blobs").exists():
        for blob in (pkg / "blobs").iterdir():
            assert b"Z" * 4096 not in blob.read_bytes()
    if (pkg / "recording.json").exists():
        sha = s.sha((pkg / "recording.json").read_bytes())
        assert s.verify_code(pkg, sha) in INCOMPLETE_CODES


def test_oversize_model_request_is_incomplete(tmp_path):
    def driver(hooks: Any):
        model = hooks.model(s.FakeModel([s.reply(content="ok")]))
        hooks.clock(s.START + timedelta(minutes=1))
        model.complete(
            ModelCall(
                messages=({"role": "user", "content": "y" * (8 * MiB + 1)},),
                tools=None,
                json_mode=False,
                max_tokens=10,
                timeout_seconds=30.0,
            )
        )
        return s.outcome(())

    # I:69: single request 8 MiB.
    assert record_code(tmp_path, driver) == "RECORDING_INCOMPLETE"
    assert_not_complete(tmp_path / "pkg")


def test_response_too_large_marks_incomplete_even_if_driver_swallows_it(tmp_path):
    def driver(hooks: Any):
        model = hooks.model(s.FakeModel([ModelError("RESPONSE_TOO_LARGE")]))
        hooks.clock(s.START + timedelta(minutes=1))
        try:
            model.complete(
                ModelCall(
                    messages=({"role": "user", "content": "q"},),
                    tools=None,
                    json_mode=False,
                    max_tokens=10,
                    timeout_seconds=30.0,
                )
            )
        except Exception:
            pass
        return s.outcome(())

    # I:94: RESPONSE_TOO_LARGE is incomplete.
    assert record_code(tmp_path, driver) == "RECORDING_INCOMPLETE"
    assert_not_complete(tmp_path / "pkg")


def test_business_clock_cannot_go_backwards(tmp_path):
    def driver(hooks: Any):
        hooks.clock(s.START + timedelta(minutes=5))
        hooks.clock(s.START + timedelta(minutes=4))
        return s.outcome(())

    # I:56: clock must not regress; I:62 clock monotonic.
    with pytest.raises(RecordingError):
        make(tmp_path, driver)
    assert_not_complete(tmp_path / "pkg")


def test_finish_seals_business_clock_and_overrides_ended_at(tmp_path):
    sealed = s.START + timedelta(minutes=9)

    def driver(hooks: Any):
        result = s.source_driver()(hooks)
        hooks.finish(sealed)
        return result

    pkg, ref = make(tmp_path, driver, ended_at=s.START)
    # I:56: finish(at) seals with the last business clock reading and
    # overrides context.ended_at (input ended_at may be started_at).
    assert loaded(pkg, ref).context.ended_at == sealed


def test_events_after_finish_are_refused(tmp_path):
    def driver(hooks: Any):
        hooks.clock(s.START + timedelta(minutes=1))
        hooks.finish(s.START + timedelta(minutes=2))
        model = hooks.model(s.FakeModel([s.reply(content="late")]))
        model.complete(
            ModelCall(
                messages=({"role": "user", "content": "q"},),
                tools=None,
                json_mode=False,
                max_tokens=10,
                timeout_seconds=30.0,
            )
        )
        return s.outcome(())

    # I:56: after sealing, new events are refused.
    with pytest.raises(RecordingError):
        make(tmp_path, driver)
    assert_not_complete(tmp_path / "pkg")


def test_control_event_is_schema_invalid_and_incomplete(tmp_path):
    def driver(hooks: Any):
        hooks.clock(s.START + timedelta(minutes=1))
        hooks.control({"kind": "pause", "at": s.START.isoformat()})
        return s.outcome(())

    # I:56: control(event) unconditionally raises SCHEMA_INVALID, marks incomplete;
    # C:55 (ruling B2): unrepresentable control events reject the fixture.
    assert record_code(tmp_path, driver) == "SCHEMA_INVALID"
    pkg = tmp_path / "pkg"
    assert_not_complete(pkg)
    if (pkg / "recording.json").exists():
        sha = s.sha((pkg / "recording.json").read_bytes())
        assert s.verify_code(pkg, sha) is not None


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"final_state": "paused"}, id="paused"),
        pytest.param({"final_state": "cancelled"}, id="cancelled"),
        pytest.param({"human_interaction": "pause"}, id="pause-interaction"),
        pytest.param({"human_interaction": "follow_up"}, id="follow-up-interaction"),
    ],
)
def test_human_control_terminal_fixture_is_rejected(tmp_path, changes):
    result = s.outcome(("ev-1",), **changes)
    # I:94: known human-control end states/interactions rejected like control events.
    assert record_code(tmp_path, s.source_driver(result=result)) == "SCHEMA_INVALID"
    assert_not_complete(tmp_path / "pkg")


# --- reader: paths, symlinks, hashes, schema ---------------------------------------


def test_wrong_expected_hash_is_hash_mismatch(source):
    pkg, ref, _ = source
    # I:81: recording expected_hash mismatch.
    assert s.verify_code(pkg, "0" * 64) == "HASH_MISMATCH"
    assert s.verify_code(pkg, ref.sha256) is None


def test_flipped_blob_byte_is_hash_mismatch(source):
    pkg, ref, _ = source
    event = s.events_of(s.package_json(pkg), "tool")[0]
    path = pkg / event["response_ref"]["path"]
    data = bytearray(path.read_bytes())
    data[-2] ^= 0x01
    path.chmod(0o600)
    path.write_bytes(bytes(data))
    # I:69 per-blob bytes/SHA; I:81 blob SHA mismatch.
    assert s.verify_code(pkg, ref.sha256) == "HASH_MISMATCH"


def test_missing_blob_is_evidence_unavailable(source):
    pkg, ref, _ = source
    event = s.events_of(s.package_json(pkg), "tool")[0]
    (pkg / event["response_ref"]["path"]).unlink()
    # I:85: missing reference.
    assert s.verify_code(pkg, ref.sha256) == "EVIDENCE_UNAVAILABLE"


def test_blob_symlink_is_rejected_even_with_identical_bytes(source, tmp_path):
    pkg, ref, _ = source
    event = s.events_of(s.package_json(pkg), "tool")[0]
    path = pkg / event["response_ref"]["path"]
    copy = tmp_path / "outside-copy"
    copy.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(copy)
    # I:69 reject any symlink; I:85 symlink -> EVIDENCE_UNAVAILABLE.
    assert s.verify_code(pkg, ref.sha256) == "EVIDENCE_UNAVAILABLE"


def test_symlinked_blobs_directory_is_rejected(source, tmp_path):
    pkg, ref, _ = source
    moved = tmp_path / "moved-blobs"
    (pkg / "blobs").rename(moved)
    (pkg / "blobs").symlink_to(moved, target_is_directory=True)
    # I:69: any symlink on the path.
    assert s.verify_code(pkg, ref.sha256) == "EVIDENCE_UNAVAILABLE"


def test_symlinked_recording_json_is_rejected(source):
    pkg, ref, _ = source
    (pkg / "recording.json").rename(pkg / "recording.real")
    (pkg / "recording.json").symlink_to(pkg / "recording.real")
    # I:69 / I:85.
    assert s.verify_code(pkg, ref.sha256) == "EVIDENCE_UNAVAILABLE"


def test_non_regular_blob_is_rejected(source):
    pkg, ref, _ = source
    event = s.events_of(s.package_json(pkg), "tool")[0]
    path = pkg / event["response_ref"]["path"]
    path.unlink()
    path.mkdir()
    # I:69 non-regular file; I:85.
    assert s.verify_code(pkg, ref.sha256) == "EVIDENCE_UNAVAILABLE"


@pytest.mark.parametrize(
    "make_path",
    [
        pytest.param(lambda pkg, name: f"../{name}", id="parent-escape"),
        pytest.param(lambda pkg, name: str(pkg.parent / name), id="absolute"),
        pytest.param(lambda pkg, name: f"blobs/../../{name}", id="dotdot-segment"),
        pytest.param(lambda pkg, name: f"blobs\\{name}", id="backslash"),
    ],
)
def test_out_of_package_ref_path_is_schema_invalid(source, make_path):
    pkg, _, _ = source
    doc = s.package_json(pkg)
    ref = s.events_of(doc, "tool")[0]["response_ref"]
    name = "escaped-blob"
    # Same bytes outside the package so only the path rule can reject it.
    (pkg.parent / name).write_bytes(s.blob_bytes(pkg, ref))
    ref["path"] = make_path(pkg, name)
    sha = s.write_recording(pkg, doc)
    # I:69 path traversal rejected; I:83 illegal path -> SCHEMA_INVALID.
    assert s.verify_code(pkg, sha) == "SCHEMA_INVALID"


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: d.update(extra=1), id="unknown-top-field"),
        pytest.param(
            lambda d: d.update(schema="opspilot-replay-2"), id="unknown-version"
        ),
        pytest.param(lambda d: d.pop("outcome"), id="missing-field"),
        pytest.param(
            lambda d: d["events"][0].update(note="x"), id="unknown-event-field"
        ),
        pytest.param(
            lambda d: d["context"].update(note="x"), id="unknown-context-field"
        ),
        pytest.param(
            lambda d: d["events"][0]["hashes"].update(raw_sha256="0" * 64),
            id="hash-name-not-defined-for-kind",
        ),
        pytest.param(lambda d: d.update(complete="true"), id="string-bool"),
    ],
)
def test_schema_violations_are_schema_invalid(source, mutate):
    pkg, _, _ = source
    doc = s.package_json(pkg)
    mutate(doc)
    sha = s.write_recording(pkg, doc)
    # I:62 all fields required, unknown fields/versions rejected; hashes exactly
    # per kind; I:83 SCHEMA_INVALID.
    assert s.verify_code(pkg, sha) == "SCHEMA_INVALID"


def test_duplicate_json_key_is_schema_invalid(source):
    pkg, _, _ = source
    data = (pkg / "recording.json").read_bytes()
    assert data.endswith(b"}")
    tampered = data[:-1] + b',"complete":true}'
    (pkg / "recording.json").unlink()
    (pkg / "recording.json").write_bytes(tampered)
    # I:8 strict JSON; I:83.
    assert s.verify_code(pkg, s.sha(tampered)) == "SCHEMA_INVALID"


def test_complete_false_is_never_usable(source):
    pkg, _, _ = source
    doc = s.package_json(pkg)
    doc["complete"] = False
    doc["incomplete_reasons"] = ["DRIVER_INTERRUPTED"]
    sha = s.write_recording(pkg, doc)
    # I:80: complete=false must not be used as a complete package.
    assert s.verify_code(pkg, sha) in INCOMPLETE_CODES


def test_incomplete_marker_is_never_usable(source):
    pkg, ref, _ = source
    (pkg / "INCOMPLETE").write_bytes(b"")
    # I:41/I:80: an INCOMPLETE marker is never treated as complete.
    assert s.verify_code(pkg, ref.sha256) in INCOMPLETE_CODES


# --- credentials and private protocol fields ----------------------------------------


CANARY = "canary-b2-0001"
RESPONSE = json.dumps(
    {
        "id": "resp-http-1",
        "model": "deepseek-flash",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "ok",
                    "reasoning_content": "R",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3},
    }
).encode()


class _Response:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._chunks = [body]
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


class _Opener:
    def __init__(self, body: bytes = RESPONSE, *, fail: bool = False) -> None:
        self.body = body
        self.fail = fail
        self.sent: list[bytes] = []
        self.headers: list[dict[str, str]] = []

    def open(self, request: Any, timeout: float):
        self.sent.append(request.data)
        self.headers.append(dict(request.header_items()))
        if self.fail:
            raise URLError("down")
        return _Response(self.body)


CALL = ModelCall(
    messages=({"role": "user", "content": "hi"},),
    tools=None,
    json_mode=False,
    max_tokens=10,
    timeout_seconds=5.0,
)


def test_serialized_request_bytes_are_unchanged_on_default_path():
    # I:54: default path does not change serialization or request hash
    # (loop request_sha256 = sha256(serialized_request), loop.py:1071).
    assert serialized_request(CALL) == (
        b'{"model": "deepseek-flash", "messages": [{"role": "user", "content": "hi"}],'
        b' "max_tokens": 10, "thinking": {"type": "enabled"},'
        b' "reasoning_effort": "high", "stream": false}'
    )


def test_deepseek_client_default_path_posts_serialized_request():
    opener = _Opener()
    client = DeepSeekClient(CANARY, opener=opener)
    # I:54: only enabling recording adds the callback.
    assert getattr(client, "capture_http", None) is None
    client.complete(CALL)
    assert opener.sent == [serialized_request(CALL)]


def test_capture_http_gets_body_response_status_but_no_headers():
    seen: list[tuple[Any, ...]] = []
    opener = _Opener()
    client = DeepSeekClient(CANARY, opener=opener)
    client.capture_http = lambda *a, **k: seen.append(tuple(a) + tuple(k.values()))
    client.complete(CALL)
    # I:54: hook after body serialization; body/response/status only; bytes
    # sent are unchanged by the hook.
    assert opener.sent == [serialized_request(CALL)]
    assert seen == [(serialized_request(CALL), RESPONSE, 200)]
    assert CANARY not in repr(seen)


def test_capture_http_is_called_on_transport_failure():
    seen: list[tuple[Any, ...]] = []
    client = DeepSeekClient(CANARY, opener=_Opener(fail=True))
    client.capture_http = lambda *a, **k: seen.append(tuple(a) + tuple(k.values()))
    with pytest.raises(ModelError):
        client.complete(CALL)
    # I:54: called on success or failure; I:65 status unknown -> no int status.
    assert len(seen) == 1
    assert seen[0][0] == serialized_request(CALL)
    assert seen[0][1] is None and seen[0][2] is None


def test_recorded_http_exchange_has_no_credentials_and_matches_parent(tmp_path):
    opener = _Opener()

    def driver(hooks: Any):
        client = DeepSeekClient(CANARY, opener=opener)
        client.capture_http = hooks.http
        hooks.clock(s.START + timedelta(minutes=1))
        hooks.model(client).complete(CALL)
        return s.outcome(())

    pkg, ref = make(tmp_path, driver)
    everything = s.all_package_bytes(pkg)
    # C:119 / I:54: credential headers are never recorded.
    assert CANARY.encode() not in everything
    assert b"Bearer" not in everything and b"Authorization" not in everything
    doc = s.package_json(pkg)
    model = s.events_of(doc, "model")[0]
    http = s.events_of(doc, "http")[0]
    # I:62 http parent is the started model event; I:65 bytes equal the parent's.
    assert http["parent"] == model["seq"]
    assert s.blob_bytes(pkg, http["request_ref"]) == opener.sent[0]
    assert s.blob_json(pkg, model["request_ref"])["body"].encode() == opener.sent[0]
    assert s.blob_bytes(pkg, http["response_ref"]) == RESPONSE
    assert http["hashes"] == {
        "request_body_sha256": s.sha(opener.sent[0]),
        "status": "200",
    }
    assert s.verify_code(pkg, ref.sha256) is None


def test_replay_refuses_source_private_reasoning(tmp_path, source):
    _, _, rec = source
    inv = Investigator()
    holder: dict[str, Any] = {}

    def go():
        capture, _, _ = run_replay(
            tmp_path, rec, [s.REQ_A], investigator=inv, echo_reasoning=s.SRC_REASONING
        )
        holder["capture"] = capture
        return capture

    # I:58/I:92 + PRODUCT-CONSTRAINTS:48: old reasoning_content is never sent.
    assert_refused(go)
    assert all(s.SRC_REASONING not in repr(c.messages) for c in inv.calls)
    if (tmp_path / "replayed").exists():
        assert s.SRC_REASONING.encode() not in s.all_package_bytes(
            tmp_path / "replayed"
        )


def test_replay_allows_reasoning_produced_in_this_run(tmp_path, source):
    _, _, rec = source
    capture, inv, _ = run_replay(tmp_path, rec, [s.REQ_A])
    # I:58: the proxy lets this Run's own reasoning_content be echoed back.
    assert capture.error is None and capture.attempt.replay_miss is False
    assert len(inv.calls) == 2
    assert "NEW-REASONING-1" in repr(inv.calls[1].messages)


def test_replay_refuses_unknown_private_message_field(tmp_path, source):
    _, _, rec = source
    inv = Investigator()

    def inject(model: Any):
        class Wrapped:
            def complete(self, call: ModelCall):
                msgs = (dict(call.messages[0], x_private_blob="leak"),) + call.messages[
                    1:
                ]
                return model.complete(replace(call, messages=msgs))

        return Wrapped()

    # I:92: only product message fields; unknown private protocol fields refused.
    assert_refused(
        lambda: run_replay(
            tmp_path, rec, [s.REQ_A], investigator=inv, mutate_first=inject
        )[0]
    )
    assert all("x_private_blob" not in repr(c.messages) for c in inv.calls)


def test_replay_refuses_unknown_raw_protocol_in_reply(tmp_path, source):
    _, _, rec = source
    inv = s.FakeModel(
        [
            s.reply(
                calls=("new_call_0",),
                reasoning="NEW",
                raw={"id": "new-1", "usage": {}, "x_vendor_private": "p"},
            ),
            s.reply(content="done"),
        ]
    )
    # I:58: ModelReply.raw only carries normalized id/usage.
    assert_refused(lambda: run_replay(tmp_path, rec, [s.REQ_A], investigator=inv)[0])


class _EnvGuard(dict):
    def __init__(self, base: Any) -> None:
        super().__init__(base)
        self.read: list[str] = []

    def __getitem__(self, key: str) -> str:
        self.read.append(key)
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        self.read.append(key)
        return super().get(key, default)


def test_structural_mode_calls_no_client_driver_or_credentials(
    source, tmp_path, monkeypatch
):
    _, ref, rec = source
    guard = _EnvGuard(os.environ)
    monkeypatch.setattr(os, "environ", guard)
    inv = s.ForbiddenModel()
    called: list[Any] = []
    capture = replay(
        rec,
        mode="structural",
        attempt=s.attempt("structural"),
        manifest=s.manifest(),
        investigator=inv,
        driver=lambda session: called.append(session),
        destination=tmp_path / "ignored",
    )
    # I:44 / C:124: zero client calls, credentials not read, driver/destination ignored.
    assert inv.calls == 0 and called == []
    assert not (tmp_path / "ignored").exists()
    secretish = ("KEY", "TOKEN", "SECRET", "DEEPSEEK", "PASSWORD")
    assert not [k for k in guard.read if any(m in k.upper() for m in secretish)]
    # I:43-44: structural capture -> verified, no new recording_ref.
    assert capture.structural_verified is True
    assert capture.recording_ref is None
    assert capture.source_run_id == s.SOURCE_RUN
    assert capture.error is None


# --- structural byte/hash/identity verification -------------------------------------


def _rewrite_blob_json(pkg: Path, ref: dict[str, Any], change: Any) -> None:
    value = s.blob_json(pkg, ref)
    change(value)
    s.put_blob(pkg, ref, s.canonical_bytes(value))


def test_tampered_request_body_is_hash_mismatch(source):
    pkg, _, _ = source
    doc = s.package_json(pkg)
    ref = s.events_of(doc, "model")[0]["request_ref"]

    def change(value: dict[str, Any]) -> None:
        # Only whitespace changes: the body hash must still catch it (I:69).
        value["body"] = value["body"].replace('"max_tokens": ', '"max_tokens":')

    _rewrite_blob_json(pkg, ref, change)
    sha = s.write_recording(pkg, doc)
    # I:64/I:81: request body hash checked independently.
    assert s.verify_code(pkg, sha) == "HASH_MISMATCH"


def test_swapped_body_and_messages_hashes_are_hash_mismatch(source):
    pkg, _, _ = source
    doc = s.package_json(pkg)
    hashes = s.events_of(doc, "model")[0]["hashes"]
    hashes["request_body_sha256"], hashes["messages_sha256"] = (
        hashes["messages_sha256"],
        hashes["request_body_sha256"],
    )
    sha = s.write_recording(pkg, doc)
    # I:69: body, messages, raw, view hashes checked separately, never interchanged.
    assert s.verify_code(pkg, sha) == "HASH_MISMATCH"


def test_tampered_tool_raw_is_hash_mismatch(source):
    pkg, _, _ = source
    doc = s.package_json(pkg)
    ref = s.events_of(doc, "tool")[0]["response_ref"]
    _rewrite_blob_json(
        pkg, ref, lambda v: v["evidence"].update(raw=b'{"spans":[]}'.hex())
    )
    sha = s.write_recording(pkg, doc)
    # I:66/I:81: raw hash checked independently of the blob SHA.
    assert s.verify_code(pkg, sha) == "HASH_MISMATCH"


def test_tampered_delivery_view_is_hash_mismatch(source):
    pkg, _, _ = source
    doc = s.package_json(pkg)
    ref = s.events_of(doc, "delivery")[0]["request_ref"]
    _rewrite_blob_json(pkg, ref, lambda v: v.update(status="error"))
    sha = s.write_recording(pkg, doc)
    # I:67/I:81: view hash.
    assert s.verify_code(pkg, sha) == "HASH_MISMATCH"


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda d: d["events"][1].update(identity="other-run"), id="identity"
        ),
        pytest.param(lambda d: d["events"][1].update(seq=7), id="seq-gap"),
        pytest.param(
            lambda d: d["events"][0].update(
                clock=(s.START - timedelta(minutes=1)).isoformat()
            ),
            id="clock-before-start",
        ),
        pytest.param(
            lambda d: d["events"][2].update(parent=d["events"][0]["seq"]),
            id="delivery-parent-not-tool",
        ),
    ],
)
def test_identity_violations_are_identity_mismatch(source, mutate):
    pkg, _, _ = source
    doc = s.package_json(pkg)
    mutate(doc)
    sha = s.write_recording(pkg, doc)
    # I:62 seq/identity/clock/parent rules; I:82 IDENTITY_MISMATCH.
    assert s.verify_code(pkg, sha) == "IDENTITY_MISMATCH"


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"scenario_id": "other-scenario"}, id="scenario"),
        pytest.param({"mode": "replay"}, id="mode"),
        pytest.param({"manifest_hash": "0" * 64}, id="manifest-hash"),
    ],
)
def test_structural_attempt_binding_errors_are_identity_mismatch(source, changes):
    _, _, rec = source
    # I:82: attempt/manifest/mode/scenario binding errors.
    with pytest.raises(RecordingError) as err:
        s.structural(rec, attempt=replace(s.attempt("structural"), **changes))
    assert err.value.code == "IDENTITY_MISMATCH"


def test_delivery_that_differs_from_next_model_request_is_identity_mismatch(tmp_path):
    def driver(hooks: Any):
        real = hooks.delivery

        class Hooks:
            def __getattr__(self, name: str) -> Any:
                return getattr(hooks, name)

            def delivery(self, view: Any, message: Any) -> None:
                real(view, dict(message, content="not what the model got"))

        return s.source_driver()(Hooks())

    # I:67: a delivered tool message must match the following model request.
    try:
        pkg, ref = make(tmp_path, driver)
    except RecordingError as exc:
        assert exc.code == "IDENTITY_MISMATCH"
        return
    assert s.verify_code(pkg, ref.sha256) == "IDENTITY_MISMATCH"


def test_stub_delivery_records_what_was_actually_sent(tmp_path):
    log: dict[str, Any] = {}
    marker = s.FULL_VIEW_MARKER
    view = {"tool": s.TOOL, "status": "ok", "content": [{"note": marker}]}
    outcomes = [lambda req: s.tool_outcome(req, evidence_id="ev-1", view=view)]
    pkg, ref = make(tmp_path, s.source_driver(outcomes=outcomes, stub=True, log=log))
    doc = s.package_json(pkg)
    delivery = s.events_of(doc, "delivery")[0]
    final = s.events_of(doc, "model")[-1]
    # I:52/I:67/C:136: full view and actual message stored separately; the
    # stub is what was delivered, the full text is not "delivered".
    assert s.blob_json(pkg, delivery["request_ref"]) == view
    message = s.blob_json(pkg, delivery["response_ref"])
    assert message["tool_call_id"] == "src_call_0"
    assert marker not in json.dumps(message)
    assert marker.encode() not in s.blob_bytes(pkg, final["request_ref"])
    assert set(delivery["hashes"]) == {"view_sha256", "delivery_sha256"}
    assert delivery["hashes"]["view_sha256"] == canonical_view_hash(view)
    # I:67: a stub is allowed; the package still verifies.
    assert s.verify_code(pkg, ref.sha256) is None


def test_trailing_delivery_without_later_model_request_is_allowed(tmp_path):
    pkg, ref = make(tmp_path, s.source_driver(tail_delivery=True))
    doc = s.package_json(pkg)
    # I:67: trailing delivery only means generated; it does not break the package.
    assert doc["events"][-1]["kind"] == "delivery"
    assert doc["complete"] is True
    assert s.verify_code(pkg, ref.sha256) is None


# --- model mode: frozen tool lookup, miss, clock, identity -----------------------------


def test_replay_hit_returns_frozen_evidence_with_explicit_run_mapping(tmp_path, source):
    pkg, ref, rec = source
    capture, inv, log = run_replay(tmp_path, rec, [s.REQ_A])
    frozen = log["seen"][0]
    # I:44/I:90: old operation/evidence identity and view hash unchanged.
    assert frozen.operation.operation_id == "src-step-1-t0"
    assert frozen.evidence.evidence_id == "ev-1"
    assert frozen.evidence.view_sha256 == canonical_view_hash(frozen.model_view)
    # I:44: id_map new operation_id -> old operation_id.
    assert dict(capture.id_map) == {"new-step-1-t0": "src-step-1-t0"}
    # I:58 / C:55: business clock fixed at source ended_at; monotonic is real.
    assert log["now"] == rec.context.ended_at == s.END
    assert capture.business_time == s.END
    assert abs(log["mono"] - time.monotonic()) < 60
    # I:44: source/replay Run explicitly distinct and mapped.
    assert capture.source_run_id == s.SOURCE_RUN
    assert capture.replay_run_id != s.SOURCE_RUN
    assert capture.error is None
    assert capture.attempt.replay_miss is False
    assert capture.attempt.status != "aborted"
    # I:58: model timeout = min(original, 360, Run remaining).
    assert all(c.timeout_seconds <= 360 for c in inv.calls)
    # I:44/I:58: a new recording of the current actual requests.
    assert capture.recording_ref is not None
    new_pkg = tmp_path / "replayed"
    new = load_recording(
        new_pkg / capture.recording_ref.path, expected_hash=capture.recording_ref.sha256
    )
    assert new.complete is True
    sent = [e.hashes["request_body_sha256"] for e in new.events if e.kind == "model"]
    assert sent == [s.sha(serialized_request(c)) for c in inv.calls]
    # I:90: replay_source fixed in the new package's initial_state.
    assert new.context.initial_state["replay_source"] == {
        "run_id": s.SOURCE_RUN,
        "recording_hash": ref.sha256,
        "id_map": {"new-step-1-t0": "src-step-1-t0"},
    }
    # I:94: the new package copies the source input blob.
    assert [r.sha256 for r in new.context.input_refs] == [
        r.sha256 for r in rec.context.input_refs
    ]
    # C:119: source private reasoning never enters the new Run's package.
    assert s.SRC_REASONING.encode() not in s.all_package_bytes(new_pkg)


@pytest.mark.parametrize(
    "request_",
    [
        pytest.param(
            dict(s.REQ_A, params=dict(s.REQ_A["params"], limit="20")), id="type"
        ),
        pytest.param(
            dict(s.REQ_A, params=dict(s.REQ_A["params"], ids=[2, 1])), id="array-order"
        ),
        pytest.param(
            dict(
                s.REQ_A, params=dict(s.REQ_A["params"], query="service=cart error=true")
            ),
            id="query-string",
        ),
        pytest.param(
            dict(s.REQ_A, params=dict(s.REQ_A["params"], offset=0)),
            id="explicit-vs-omitted-param",
        ),
        pytest.param(
            dict(
                s.REQ_A,
                window={
                    "start": (s.START + timedelta(minutes=5)).isoformat(),
                    "end": s.END.isoformat(),
                },
            ),
            id="explicit-default-window",
        ),
        pytest.param(
            dict(s.REQ_A, params=dict(s.REQ_A["params"], step_id="src-step-1")),
            id="params-step-id-kept",
        ),
    ],
)
def test_tool_lookup_is_exact_and_any_difference_is_replay_miss(
    tmp_path, source, request_
):
    _, _, rec = source
    capture, inv, log = run_replay(tmp_path, rec, [request_])
    # C:54/C:134 (ruling B1=B), I:71: types, array order, query string,
    # explicit vs omitted fields all part of the key; no approximation.
    assert_miss(capture)
    assert log["seen"] == []
    # I:71: the Run stops -- no further model request after the miss.
    assert len(inv.calls) == 1


def test_no_model_call_after_miss_even_if_driver_continues(tmp_path, source):
    _, _, rec = source
    bad = dict(s.REQ_A, params={"query": "nothing recorded"})
    capture, inv, _ = run_replay(tmp_path, rec, [bad], swallow=True)
    # I:71 "停止该 Run" / task ruling B1: after REPLAY_MISS no model call, no live query.
    assert_miss(capture)
    assert len(inv.calls) == 1


def test_same_key_responses_are_consumed_in_source_occurrence_order(tmp_path):
    pkg, ref = make(tmp_path, s.source_driver([s.REQ_A, s.REQ_A]), name="source")
    rec = loaded(pkg, ref)
    capture, _, log = run_replay(tmp_path, rec, [s.REQ_A, s.REQ_A])
    # I:71/C:134: occurrence consumed in source order.
    assert [o.evidence.evidence_id for o in log["seen"]] == ["ev-1", "ev-2"]
    assert capture.attempt.replay_miss is False


def test_exhausted_occurrences_are_replay_miss(tmp_path):
    pkg, ref = make(tmp_path, s.source_driver([s.REQ_A, s.REQ_A]), name="source")
    rec = loaded(pkg, ref)
    capture, inv, log = run_replay(tmp_path, rec, [s.REQ_A, s.REQ_A, s.REQ_A])
    # I:79: exhausted -> REPLAY_MISS; nothing synthesized for the third call.
    assert [o.evidence.evidence_id for o in log["seen"]] == ["ev-1", "ev-2"]
    assert_miss(capture)
    assert len(inv.calls) == 1


def test_unused_recorded_tail_is_not_a_miss(tmp_path):
    pkg, ref = make(tmp_path, s.source_driver([s.REQ_A, s.REQ_B]), name="source")
    rec = loaded(pkg, ref)
    capture, inv, _ = run_replay(tmp_path, rec, [s.REQ_A])
    # I:71 extra tail items allowed; C:134 early model finish is not a miss.
    assert capture.attempt.replay_miss is False
    assert capture.attempt.status != "aborted"
    assert capture.error is None
    assert len(inv.calls) == 2


@pytest.mark.parametrize("dimension", s.COMPAT_DIMENSIONS)
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(DimensionValue("version", "v2", None), id="changed"),
        pytest.param(DimensionValue("unknown", None, "not frozen"), id="unknown"),
        pytest.param(DimensionValue("none", None, "absent"), id="none"),
    ],
)
def test_incompatible_frozen_versions_are_replay_miss(
    tmp_path, source, dimension, value
):
    _, _, rec = source
    frozen = s.manifest(**{dimension: value})
    capture, _, log = run_replay(tmp_path, rec, [s.REQ_A], frozen=frozen)
    # I:71/I:79: tools_schema/registry/profile/projection/permissions must equal
    # context.versions; unknown/none incompatible -> REPLAY_MISS.
    assert_miss(capture)
    assert log.get("seen", []) == []


@pytest.mark.parametrize("dimension", ["prompt", "model_request", "code"])
def test_prompt_model_code_may_change_in_current_manifest(tmp_path, source, dimension):
    _, _, rec = source
    frozen = s.manifest(**{dimension: DimensionValue("version", "v9", None)})
    capture, _, _ = run_replay(tmp_path, rec, [s.REQ_A], frozen=frozen)
    # I:71 / C:134: prompt/model/code frozen independently in the current manifest.
    assert capture.attempt.replay_miss is False
    assert capture.error is None


@pytest.mark.parametrize("missing", ["destination", "driver", "investigator"])
def test_model_mode_requires_destination_driver_and_investigator(
    tmp_path, source, missing
):
    _, _, rec = source
    kwargs: dict[str, Any] = {
        "investigator": Investigator(),
        "driver": replay_driver([s.REQ_A], {}),
        "destination": tmp_path / "replayed",
    }
    kwargs[missing] = None
    # I:44 required in model mode; I:83 configuration missing -> SCHEMA_INVALID.
    with pytest.raises(RecordingError) as err:
        replay(
            rec,
            mode="replay",
            attempt=s.attempt("replay"),
            manifest=s.manifest(),
            **kwargs,
        )
    assert err.value.code == "SCHEMA_INVALID"


def test_model_mode_existing_destination_is_output_exists(tmp_path, source):
    _, _, rec = source
    (tmp_path / "replayed").mkdir()
    inv = Investigator()
    # I:41 replay recordings follow the same exclusive-create rule; I:84.
    with pytest.raises(RecordingError) as err:
        run_replay(tmp_path, rec, [s.REQ_A], investigator=inv)
    assert err.value.code == "OUTPUT_EXISTS"
    assert inv.calls == []


def test_model_mode_reverifies_source_before_calling_the_model(tmp_path, source):
    pkg, _, rec = source
    event = s.events_of(s.package_json(pkg), "tool")[0]
    path = pkg / event["response_ref"]["path"]
    data = bytearray(path.read_bytes())
    data[-2] ^= 0x01
    path.chmod(0o600)
    path.write_bytes(bytes(data))
    inv = Investigator()
    # I:75: model mode fully verifies the source package first.
    with pytest.raises(RecordingError) as err:
        run_replay(tmp_path, rec, [s.REQ_A], investigator=inv)
    assert err.value.code == "HASH_MISMATCH"
    assert inv.calls == []


def test_incomplete_source_cannot_be_replayed(tmp_path, source):
    pkg, _, _ = source
    doc = s.package_json(pkg)
    doc["complete"] = False
    doc["incomplete_reasons"] = ["DRIVER_INTERRUPTED"]
    sha = s.write_recording(pkg, doc)
    inv = Investigator()
    # I:80: incomplete packages are never used as complete.
    with pytest.raises(RecordingError) as err:
        rec = load_recording(pkg / "recording.json", expected_hash=sha)
        run_replay(tmp_path, rec, [s.REQ_A], investigator=inv)
    assert err.value.code == "RECORDING_INCOMPLETE"
    assert inv.calls == []


def test_driver_failure_is_driver_error_not_quality_pass(tmp_path, source):
    _, _, rec = source

    def broken(session: Any):
        raise ValueError("driver bug")

    capture = replay(
        rec,
        mode="replay",
        attempt=s.attempt("replay"),
        manifest=s.manifest(),
        investigator=Investigator(),
        driver=broken,
        destination=tmp_path / "replayed",
    )
    # I:75/I:86: driver failure -> error=DRIVER_ERROR, never a quality pass.
    assert capture.error == "DRIVER_ERROR"
    assert capture.attempt.replay_miss is False
    if (tmp_path / "replayed" / "recording.json").exists():
        assert s.package_json(tmp_path / "replayed")["complete"] is False


def test_replay_session_exposes_frozen_context_not_source_history(tmp_path, source):
    _, _, rec = source
    _, inv, log = run_replay(tmp_path, rec, [s.REQ_A])
    # I:58: driver builds isolated state from context.stimulus/initial_state;
    # the first request carries no source model history.
    assert log["context"].stimulus == rec.context.stimulus
    assert s.SRC_REASONING not in repr(inv.calls[0].messages)
    assert "src_call_0" not in repr(inv.calls[0].messages)


# --- independent review findings (2026-10-14) --------------------------------------
# Semantics fixed by the lead after the independent review of the B2
# implementation; the interface record will be synced. See
# EVAL_B2_AMBIGUITIES.md items 14-16 for the readings adopted here.


def _window(start_min: int, end_min: int) -> dict[str, str]:
    return {
        "start": s.iso(s.START + timedelta(minutes=start_min)),
        "end": s.iso(s.START + timedelta(minutes=end_min)),
    }


def _forged_outcome(
    *,
    requested_tool: str | None = None,
    requested_target: str | None = None,
    view_operation_id: str | None = None,
    window: tuple[int, int] | None = None,
):
    """Outcome factory whose operation contradicts its own ToolRequest.

    Every artifact stays internally hash-consistent (normal record() path);
    only the cross-event relation between request and result is wrong.
    """

    def factory(request: Any):
        view = None
        if view_operation_id is not None:
            view = {
                "tool": s.TOOL,
                "status": "ok",
                "evidence_id": "ev-1",
                "operation_id": view_operation_id,
                "content": [{"service": "cart", "error": True}],
            }
        result = s.tool_outcome(request, evidence_id="ev-1", view=view)
        changes: dict[str, Any] = {}
        if requested_tool is not None:
            changes["requested_tool"] = requested_tool
        if requested_target is not None:
            changes["requested_target"] = requested_target
        if window is not None:
            changes["window"] = s.ToolWindow(
                s.START + timedelta(minutes=window[0]),
                s.START + timedelta(minutes=window[1]),
            )
        if changes:
            op = replace(result.operation, **changes)
            evidence = (
                None
                if result.evidence is None
                else replace(result.evidence, operation=op)
            )
            result = replace(result, operation=op, evidence=evidence)
        return result

    return factory


# Review P2-1: request/result identity and window must be cross-checked.
@pytest.mark.parametrize(
    ("request_", "factory"),
    [
        pytest.param(
            s.REQ_A,
            _forged_outcome(requested_tool="metrics.range_query"),
            id="requested-tool-differs",
        ),
        pytest.param(
            s.REQ_A,
            _forged_outcome(requested_target="other-target"),
            id="requested-target-differs",
        ),
        pytest.param(
            s.REQ_A,
            _forged_outcome(view_operation_id="OTHER-RUN-operation"),
            id="view-operation-id-differs",
        ),
        pytest.param(
            # The review's counterexample: request 12:00-12:01, result
            # 12:05-12:10; both inside the authorized box 12:00-12:10.
            dict(s.REQ_A, window=_window(0, 1)),
            _forged_outcome(window=(5, 10)),
            id="explicit-window-differs",
        ),
        pytest.param(
            # No window in the request: the result must carry the default
            # window (binding default 12:05-12:10); 12:01-12:03 is inside
            # the authorized box but is not the default.
            dict(s.REQ_A, window=None),
            _forged_outcome(window=(1, 3)),
            id="omitted-window-not-default",
        ),
    ],
)
def test_review_p2_1_contradicting_request_and_result_is_identity_mismatch(
    tmp_path, request_, factory
):
    # Review P2-1; C:124/C:134-136, I:75/I:82: structural verification
    # relates requested_tool/requested_target, an existing view.operation_id
    # and the request window with the recorded operation.
    pkg = tmp_path / "source"
    recorded: Any = None
    try:
        recorded = record(
            s.source_driver([request_], outcomes=[factory]),
            destination=pkg,
            context=s.context(),
        )
    except RecordingError as exc:
        # Detection already at record time is accepted (ambiguity 14); the
        # package is then never complete.
        assert str(exc) == exc.code
        assert exc.code == "IDENTITY_MISMATCH"
        assert_not_complete(pkg)
    if recorded is not None:
        assert s.verify_code(pkg, recorded.sha256) == "IDENTITY_MISMATCH"
        expected = "IDENTITY_MISMATCH"
        sha = recorded.sha256
    else:
        expected = "RECORDING_INCOMPLETE"
        sha = s.sha((pkg / "recording.json").read_bytes())
    # Model replay must not use the package: refused before any model call.
    inv = Investigator()
    with pytest.raises(RecordingError) as err:
        rec = load_recording(pkg / "recording.json", expected_hash=sha)
        run_replay(tmp_path, rec, [request_], investigator=inv)
    assert err.value.code == expected
    assert inv.calls == []


# Review P2-1 (control): a consistent explicit window still verifies, so the
# window check is equality, not "any window inside the authorized box".
def test_review_p2_1_explicit_window_equal_to_result_window_verifies(tmp_path):
    request_ = dict(s.REQ_A, window=_window(2, 4))
    pkg, ref = make(
        tmp_path,
        s.source_driver([request_], outcomes=[_forged_outcome(window=(2, 4))]),
    )
    assert s.verify_code(pkg, ref.sha256) is None


def _duplicate_loop_replies() -> list[Any]:
    # Same tool, same arguments, different call_id: the second call is the
    # loop's DUPLICATE_TOOL_CALL refusal (no facade call).
    return [
        loop_reply(tool_calls=[loop_tool_call("call-one")], finish="tool_calls"),
        loop_reply(tool_calls=[loop_tool_call("call-two")], finish="tool_calls"),
        report_from_transcript,
    ]


def _loop_driver(
    log: dict[str, Any], *, replay_session: bool, replay_run_id: str = s.REPLAY_RUN
):
    if replay_session:
        return _loop_replay_driver(log, replay_run_id)

    def wire(model: Any, tools: Any, deliver: Any) -> Any:
        loop, request, scripted, *_ = assemble(
            replies=_duplicate_loop_replies(),
            model_requests=3,
        )
        facade_calls: list[Any] = []
        inner = tools(loop.executor)

        class CountingFacade:
            def execute(self, tool_request: Any) -> Any:
                facade_calls.append(tool_request)
                return inner.execute(tool_request)

            def __getattr__(self, name: str) -> Any:
                return getattr(inner, name)

        loop.model = model(scripted)
        loop.executor = CountingFacade()
        loop.capture_delivery = deliver
        result = loop.run(request)
        log.update(result=result, facade_calls=facade_calls, scripted=scripted)
        return s.outcome(tuple(dict.fromkeys(result.evidence_ids)))

    return lambda hooks: wire(hooks.model, hooks.tools, hooks.delivery)


def _loop_replay_driver(log: dict[str, Any], replay_run_id: str):
    """I:58: the driver builds its own isolated Run state (scope, store, clock)
    for the explicit replay Run (I:44/I:102); session.tools only serves frozen
    tool results, so the executor adapter supplies the scope itself."""

    def driver(session: Any) -> Any:
        loop, request, scripted, *_ = assemble(replies=[], model_requests=3)
        # The remaining authorization config comes from the fixed fixture.
        scope = replace(request.scope, run_id=replay_run_id)
        facade_calls: list[Any] = []

        def execute(tool_request: Any) -> Any:
            facade_calls.append(tool_request)
            return session.tools.execute(tool_request)

        loop.clock = session.clock
        loop.store = MemoryStepStore(
            budget_limit=4,
            deadline=scope.deadline,
            clock=session.clock,
            run_id=replay_run_id,
        )
        loop.executor = SimpleNamespace(
            scope=scope, tool_seconds_used=0.0, execute=execute
        )
        loop.model = session.model
        loop.capture_delivery = session.capture_delivery
        request = replace(
            request,
            run_id=replay_run_id,
            scope=scope,
            evidence_context={**request.evidence_context, "run_id": replay_run_id},
        )
        result = loop.run(request)
        log.update(result=result, facade_calls=facade_calls, scripted=scripted)
        return s.outcome(tuple(dict.fromkeys(result.evidence_ids)))

    return driver


def _loop_context():
    loop, request, *_ = assemble(replies=[], model_requests=3)
    window = request.scope.window.as_json()
    now = loop.clock.now()
    return s.context(
        source_run_id=request.run_id,
        started_at=now,
        ended_at=now,
        bindings=(
            s.binding(
                tool="metrics.range_query",
                target="checkout-prod",
                source="prometheus",
                authorized_window=window,
                default_window=window,
            ),
        ),
    )


def _without_operation_id(view: Any) -> dict[str, Any]:
    return {k: v for k, v in dict(view).items() if k != "operation_id"}


# Review P2-2: the real InvestigationLoop's DUPLICATE_TOOL_CALL refusal of a
# repeated call (same arguments, new call_id) records as a complete package
# and replays the refusal unchanged.
def test_review_p2_2_duplicate_call_refusal_records_and_replays(tmp_path):
    log: dict[str, Any] = {}
    pkg = tmp_path / "source"
    ref = record(
        _loop_driver(log, replay_session=False),
        destination=pkg,
        context=_loop_context(),
    )
    assert log["result"].execution == "completed"
    # C:117 / I:52: one facade call; the refusal never reaches the facade.
    assert len(log["facade_calls"]) == 1
    doc = s.package_json(pkg)
    assert doc["complete"] is True and doc["incomplete_reasons"] == []
    assert len(s.events_of(doc, "model")) == 3
    assert len(s.events_of(doc, "tool")) == 1
    deliveries = s.events_of(doc, "delivery")
    assert len(deliveries) == 2
    first_view = s.blob_json(pkg, deliveries[0]["request_ref"])
    refusal_view = s.blob_json(pkg, deliveries[1]["request_ref"])
    refusal_message = s.blob_json(pkg, deliveries[1]["response_ref"])
    # I:112: CaptureHooks.delivery adds a kind='refusal' event before the
    # refused delivery; delivery.parent -> refusal -> the first tool event.
    by_seq = {event["seq"]: event for event in doc["events"]}
    refusal = by_seq[deliveries[1]["parent"]]
    assert refusal["kind"] == "refusal"
    assert refusal["seq"] < deliveries[1]["seq"]
    first_tool = s.events_of(doc, "tool")[0]
    assert refusal["parent"] == first_tool["seq"]
    assert set(refusal["hashes"]) == {"view_sha256"}
    assert refusal["error"] is None
    refusal_request = s.blob_json(pkg, refusal["request_ref"])
    assert set(refusal_request) == {
        "operation_id",
        "tool_call_id",
        "duplicate_of_evidence_id",
    }
    assert all(isinstance(v, str) and v for v in refusal_request.values())
    assert refusal_request["tool_call_id"] == "call-two"
    assert refusal_request["duplicate_of_evidence_id"] == first_view["evidence_id"]
    # The real loop's first view carries the executed call's operation_id.
    assert refusal_request["operation_id"] != first_view["operation_id"]
    assert refusal_request["operation_id"] == refusal_view["operation_id"]
    assert refusal_view["reason"] == "DUPLICATE_TOOL_CALL"
    assert refusal_view["duplicate_of_evidence_id"] == first_view["evidence_id"]
    assert refusal_message["tool_call_id"] == "call-two"
    # The refusal has its own operation_id, distinct from the executed call's.
    assert isinstance(refusal_view["operation_id"], str)
    assert refusal_view["operation_id"]
    assert refusal_view["operation_id"] != first_view["operation_id"]
    # Structural verification accepts the package.
    assert s.verify_code(pkg, ref.sha256) is None

    # Model replay reproduces the refusal from frozen evidence.
    rec = loaded(pkg, ref)
    investigator = ScriptedModel(_duplicate_loop_replies())
    replay_log: dict[str, Any] = {}
    capture = replay(
        rec,
        mode="replay",
        attempt=s.attempt("replay"),
        manifest=s.manifest(),
        investigator=investigator,
        driver=_loop_driver(replay_log, replay_session=True),
        destination=tmp_path / "replayed",
    )
    assert capture.error is None
    assert capture.attempt.replay_miss is False
    assert replay_log["result"].execution == "completed"
    assert len(investigator.calls) == 3
    assert len(replay_log["facade_calls"]) == 1
    new_pkg = tmp_path / "replayed"
    new = load_recording(
        new_pkg / capture.recording_ref.path,
        expected_hash=capture.recording_ref.sha256,
    )
    assert new.complete is True
    new_doc = s.package_json(new_pkg)
    new_deliveries = s.events_of(new_doc, "delivery")
    assert len(new_deliveries) == 2
    # I:112 applies to the new recording too: delivery.parent -> refusal.
    new_by_seq = {event["seq"]: event for event in new_doc["events"]}
    assert new_by_seq[new_deliveries[1]["parent"]]["kind"] == "refusal"
    replayed_view = s.blob_json(new_pkg, new_deliveries[1]["request_ref"])
    replayed_message = s.blob_json(new_pkg, new_deliveries[1]["response_ref"])
    # Unchanged apart from the per-Run operation_id the loop derives from
    # its new step (I:73 only new operation IDs are ignored).
    assert _without_operation_id(replayed_view) == _without_operation_id(refusal_view)
    assert replayed_message["tool_call_id"] == refusal_message["tool_call_id"]
    assert _without_operation_id(
        json.loads(replayed_message["content"])
    ) == _without_operation_id(json.loads(refusal_message["content"]))


class _TruncatedErrorBody:
    def read(self, size: int = -1) -> bytes:
        raise IncompleteRead(b"partial")

    def close(self) -> None:
        return None


class _TruncatedErrorOpener:
    def open(self, request: Any, timeout: float):
        raise HTTPError(
            "https://api.deepseek.com/v1/chat/completions",
            503,
            "error",
            {},  # type: ignore[arg-type]
            _TruncatedErrorBody(),  # type: ignore[arg-type]
        )


# Review P2-3: an HTTP 503 whose error body is cut short (IncompleteRead) is
# a known truncation, never a complete empty response body.
def test_review_p2_3_truncated_http_error_body_is_incomplete(tmp_path):
    client_errors: list[ModelError] = []

    def driver(hooks: Any):
        client = DeepSeekClient(CANARY, opener=_TruncatedErrorOpener())
        client.capture_http = hooks.http
        hooks.clock(s.START + timedelta(minutes=1))
        try:
            hooks.model(client).complete(CALL)
        except ModelError as exc:
            client_errors.append(exc)
        return s.outcome(
            (), final_state="failed", decision="handoff", report_available=False
        )

    pkg = tmp_path / "source"
    try:
        ref = record(driver, destination=pkg, context=s.context())
    except RecordingError as exc:
        # I:41: record may raise its fixed code after writing complete=false.
        assert str(exc) == exc.code
        doc = s.package_json(pkg)
        sha = s.sha((pkg / "recording.json").read_bytes())
    else:
        doc = s.package_json(pkg)
        sha = ref.sha256
    # The client itself failed the call (503); the driver handled it.
    assert len(client_errors) == 1
    # C:120 / I:69 / I:80: complete=False with a reason; the reason code
    # itself is not yet fixed and is not asserted.
    assert doc["complete"] is False
    assert doc["incomplete_reasons"]
    assert s.verify_code(pkg, sha) in INCOMPLETE_CODES


# --- independent recheck findings (2026-10-14) ----------------------------------------
# Semantics fixed by the lead after the B2 recheck; interface sync pending.
# See EVAL_B2_AMBIGUITIES.md item 17.


def _record_real_loop(tmp_path: Path, replies: list[Any], *, model_requests: int):
    """Record one real InvestigationLoop Run through CaptureHooks."""
    log: dict[str, Any] = {}

    def driver(hooks: Any) -> Any:
        loop, request, scripted, *_ = assemble(
            replies=replies, model_requests=model_requests
        )
        inner = hooks.tools(loop.executor)
        facade_calls: list[Any] = []

        class CountingFacade:
            def execute(self, tool_request: Any) -> Any:
                facade_calls.append(tool_request)
                return inner.execute(tool_request)

            def __getattr__(self, name: str) -> Any:
                return getattr(inner, name)

        loop.model = hooks.model(scripted)
        loop.executor = CountingFacade()
        loop.capture_delivery = hooks.delivery
        result = loop.run(request)
        log.update(result=result, facade_calls=facade_calls, scripted=scripted)
        return s.outcome(tuple(dict.fromkeys(result.evidence_ids)))

    pkg = tmp_path / "source"
    ref = record(driver, destination=pkg, context=_loop_context())
    return pkg, ref, log


def _delivery_by_call_id(pkg: Path, doc: dict[str, Any]) -> dict[str, Any]:
    return {
        s.blob_json(pkg, event["response_ref"])["tool_call_id"]: event
        for event in s.events_of(doc, "delivery")
    }


# Recheck P2-1: one reply [A(expr=up), duplicate A, B(expr=down)], the next
# reply duplicates B. Each refusal claims its own call ID, so B's tool event
# stays paired with B's call.
def test_recheck_p2_1_mixed_refusals_record_completely_and_pair_b(tmp_path):
    replies = [
        loop_reply(
            tool_calls=[
                loop_tool_call("a", arguments='{"expr":"up"}'),
                loop_tool_call("a-duplicate", arguments='{"expr":"up"}'),
                loop_tool_call("b", arguments='{"expr":"down"}'),
            ],
            finish="tool_calls",
        ),
        loop_reply(
            tool_calls=[loop_tool_call("b-duplicate", arguments='{"expr":"down"}')],
            finish="tool_calls",
        ),
        report_from_transcript,
    ]
    pkg, ref, log = _record_real_loop(tmp_path, replies, model_requests=3)
    assert log["result"].execution == "completed"
    assert len(log["scripted"].calls) == 3
    # Only the two physical calls reach the facade.
    assert [r.params for r in log["facade_calls"]] == [
        {"expr": "up"},
        {"expr": "down"},
    ]
    doc = s.package_json(pkg)
    assert doc["complete"] is True and doc["incomplete_reasons"] == []
    by_seq = {event["seq"]: event for event in doc["events"]}
    tools = s.events_of(doc, "tool")
    assert [s.blob_json(pkg, t["request_ref"])["request"]["params"] for t in tools] == [
        {"expr": "up"},
        {"expr": "down"},
    ]
    tool_a, tool_b = tools
    assert len(s.events_of(doc, "refusal")) == 2
    deliveries = _delivery_by_call_id(pkg, doc)
    assert set(deliveries) == {"a", "a-duplicate", "b", "b-duplicate"}
    # I:62: physical deliveries are parented by their own tool events.
    assert deliveries["a"]["parent"] == tool_a["seq"]
    assert deliveries["b"]["parent"] == tool_b["seq"]
    # I:112: delivery -> refusal -> the tool event whose evidence it cites.
    for call_id, source_tool in (("a-duplicate", tool_a), ("b-duplicate", tool_b)):
        refusal = by_seq[deliveries[call_id]["parent"]]
        assert refusal["kind"] == "refusal"
        assert refusal["parent"] == source_tool["seq"]
        assert s.blob_json(pkg, refusal["request_ref"])["tool_call_id"] == call_id
    b_view = s.blob_json(pkg, deliveries["b"]["request_ref"])
    b_refusal = by_seq[deliveries["b-duplicate"]["parent"]]
    assert (
        s.blob_json(pkg, b_refusal["request_ref"])["duplicate_of_evidence_id"]
        == b_view["evidence_id"]
    )
    assert s.verify_code(pkg, ref.sha256) is None


# Recheck P2-2: every refused delivery needs its own refusal event; an old
# refusal view cannot be reused through the cache branch.
def test_recheck_p2_2_refused_delivery_without_own_refusal_is_identity_mismatch(
    tmp_path,
):
    replies = [
        loop_reply(tool_calls=[loop_tool_call(call_id)], finish="tool_calls")
        for call_id in ("one", "two", "three")
    ] + [report_from_transcript]
    pkg, ref, log = _record_real_loop(tmp_path, replies, model_requests=4)
    assert log["result"].execution == "completed"
    assert s.verify_code(pkg, ref.sha256) is None  # control: untampered

    doc = s.package_json(pkg)
    refusals = s.events_of(doc, "refusal")
    assert len(refusals) == 2
    old_view = s.blob_json(pkg, refusals[0]["response_ref"])
    # The third delivery reuses the first refusal's view and operation_id.
    last_delivery = s.events_of(doc, "delivery")[-1]
    view_bytes = s.canonical_bytes(old_view)
    s.put_blob(pkg, last_delivery["request_ref"], view_bytes)
    message = s.blob_json(pkg, last_delivery["response_ref"])
    assert message["tool_call_id"] == "three"
    message["content"] = view_bytes.decode()
    s.put_blob(pkg, last_delivery["response_ref"], s.canonical_bytes(message))
    last_delivery["hashes"] = {
        "view_sha256": canonical_view_hash(old_view),
        "delivery_sha256": s.sha(s.canonical_bytes(message)),
    }
    last_delivery["parent"] = None
    # Keep the next actual model request consistent with the new message.
    last_model = s.events_of(doc, "model")[-1]
    body = s.blob_json(pkg, last_model["request_ref"])
    for item in body["call"]["messages"]:
        if item.get("role") == "tool" and item.get("tool_call_id") == "three":
            item["content"] = message["content"]
    body["body"] = serialized_request(decode(ModelCall, body["call"])).decode()
    s.put_blob(pkg, last_model["request_ref"], s.canonical_bytes(body))
    last_model["hashes"] = {
        "request_body_sha256": s.sha(body["body"].encode()),
        "messages_sha256": messages_hash(body["call"]["messages"]),
    }
    # Drop the second refusal event and renumber seq/parent.
    removed = refusals[-1]["seq"]
    doc["events"] = [e for e in doc["events"] if e["seq"] != removed]
    for seq, event in enumerate(doc["events"]):
        event["seq"] = seq
        if event["parent"] is not None and event["parent"] > removed:
            event["parent"] -= 1
    sha = s.write_recording(pkg, doc)
    # I:112-113: two refused deliveries but only one refusal event.
    assert s.verify_code(pkg, sha) == "IDENTITY_MISMATCH"


# Recheck P2-3: product-legal refusals that never query the source
# (operation.window=None) record completely. This narrows the P2-1 window
# rule to executed queries; the P2-1 counterexamples above still apply.
@pytest.mark.parametrize(
    ("window", "status", "reason"),
    [
        pytest.param(
            {"start": "invalid", "end": "invalid"},
            "error",
            "INVALID_PARAMS",
            id="invalid-window",
        ),
        pytest.param(None, "denied", "WINDOW_TOO_LARGE", id="window-too-large"),
    ],
)
def test_recheck_p2_3_unexecuted_tool_refusal_records_completely(
    tmp_path, window, status, reason
):
    executor, transport, _sink, clock = build_tool_executor(
        registrations=[tool_registration(max_window_seconds=300)]
    )
    # Default fixture request: a 10-minute window inside the authorized box,
    # above the 300 s tool limit.
    overrides = {} if window is None else {"window": window}
    tool_request = tool_request_fixture(**overrides)
    seen: list[Any] = []

    def driver(hooks: Any):
        model = hooks.model(
            ScriptedModel(
                [
                    loop_reply(
                        tool_calls=[
                            loop_tool_call(
                                "call",
                                tool_request.tool_name,
                                json.dumps(tool_request.params),
                            )
                        ],
                        finish="tool_calls",
                    ),
                    loop_reply(content="done"),
                ]
            )
        )
        first = ModelCall(
            messages=(
                {"role": "system", "content": s.SYSTEM},
                {"role": "user", "content": s.QUESTION},
            ),
            tools=None,
            json_mode=False,
            max_tokens=100,
            timeout_seconds=30.0,
        )
        answer = model.complete(first)
        result = hooks.tools(executor).execute(tool_request)
        seen.append(result)
        message = {
            "role": "tool",
            "tool_call_id": "call",
            "content": s.canonical_bytes(dict(result.model_view)).decode(),
        }
        hooks.delivery(result.model_view, message)
        assistant = {
            "role": "assistant",
            "content": answer.content,
            "reasoning_content": answer.reasoning_content,
            "tool_calls": [dict(c) for c in answer.tool_calls],
        }
        model.complete(replace(first, messages=(*first.messages, assistant, message)))
        return s.outcome(())

    context = s.context(
        source_run_id=executor.scope.run_id,
        started_at=clock.now(),
        ended_at=clock.now() + timedelta(seconds=10),
        bindings=(
            s.binding(
                tool=tool_request.tool_name,
                target=tool_request.target_ref,
                source="prometheus",
                authorized_window=executor.scope.window.as_json(),
                default_window=None,
            ),
        ),
    )
    pkg = tmp_path / "source"
    ref = record(driver, destination=pkg, context=context)
    # The product refused before any source query.
    (result,) = seen
    assert (result.status, result.reason) == (status, reason)
    assert result.operation.window is None
    assert transport.called is False
    doc = s.package_json(pkg)
    assert doc["complete"] is True and doc["incomplete_reasons"] == []
    assert len(s.events_of(doc, "tool")) == 1
    assert s.verify_code(pkg, ref.sha256) is None


# --- second recheck finding (2026-10-14) ----------------------------------------------
# Lead ruling: dispatched=True always gets the window relation checked, with
# or without evidence; dispatched=True + evidence=None while the view still
# carries source data (rows/evidence_id) is self-contradictory. Only a
# dispatched=False refusal with no evidence and no data skips the window
# check. See EVAL_B2_AMBIGUITIES.md item 18.

_AUTH_START = datetime(2026, 10, 12, 23, 0, tzinfo=s.START.tzinfo)
_AUTH_END = datetime(2026, 10, 13, 0, 0, tzinfo=s.START.tzinfo)


def _evidence_free_dispatched_outcome(result_start: datetime):
    def factory(request: Any):
        result = s.tool_outcome(request, evidence_id="evidence-old")
        window = s.ToolWindow(result_start, _AUTH_END)
        op = replace(result.operation, window=window)
        assert op.dispatched is True
        view = {
            **dict(result.model_view),
            "evidence_id": "evidence-old",
            "rows": [{"value": 3}],
            "window": {"start": s.iso(window.start), "end": s.iso(window.end)},
        }
        return replace(
            result,
            status="ok",
            source_contact="confirmed",
            operation=op,
            evidence=None,
            model_view=view,
        )

    return factory


# Recheck2 P2: an executed query without EvidenceRecord cannot carry data
# past the window relation check.
@pytest.mark.parametrize(
    "result_start",
    [
        # (a) the recheck counterexample: request 23:00-00:00, operation and
        # view window 23:01-00:00, both inside the authorized box.
        pytest.param(_AUTH_START + timedelta(minutes=1), id="window-differs"),
        # (b) same windows, still self-contradictory: dispatched with data
        # in the view but no EvidenceRecord.
        pytest.param(_AUTH_START, id="window-equal"),
    ],
)
def test_recheck2_p2_dispatched_result_without_evidence_but_with_data_is_rejected(
    tmp_path, result_start
):
    auth = {"start": s.iso(_AUTH_START), "end": s.iso(_AUTH_END)}
    request_ = dict(s.REQ_A, window=auth)
    context = s.context(
        bindings=(s.binding(authorized_window=auth, default_window=None),)
    )
    pkg = tmp_path / "source"
    recorded: Any = None
    try:
        recorded = record(
            s.source_driver(
                [request_], outcomes=[_evidence_free_dispatched_outcome(result_start)]
            ),
            destination=pkg,
            context=context,
        )
    except RecordingError as exc:
        # Record-time detection accepted (ambiguity 14); never complete.
        assert str(exc) == exc.code
        assert exc.code == "IDENTITY_MISMATCH"
        assert_not_complete(pkg)
    if recorded is not None:
        assert s.verify_code(pkg, recorded.sha256) == "IDENTITY_MISMATCH"
        expected = "IDENTITY_MISMATCH"
        sha = recorded.sha256
    else:
        expected = "RECORDING_INCOMPLETE"
        sha = s.sha((pkg / "recording.json").read_bytes())
    # Model replay refuses before calling the current model.
    inv = Investigator()
    with pytest.raises(RecordingError) as err:
        rec = load_recording(pkg / "recording.json", expected_hash=sha)
        run_replay(tmp_path, rec, [request_], investigator=inv)
    assert err.value.code == expected
    assert inv.calls == []
