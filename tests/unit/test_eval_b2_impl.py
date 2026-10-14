"""Synthetic PR-B2 boundary/negative cases; no model, service or credential IO."""

import hashlib
import io
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError

import pytest

from opspilot.acceptance import IncidentOutcome
from opspilot.evaluation import (
    REQUIRED_DIMENSIONS,
    AttemptRecord,
    DimensionValue,
    Manifest,
    RecordingContext,
    RecordingError,
    ToolBinding,
    load_recording,
    manifest_hash,
    record,
    replay,
)
from opspilot.evaluation.codec import canonical_bytes, encode
from opspilot.evaluation.recording import json_blob, sha
from opspilot.investigation.client import DeepSeekClient
from opspilot.investigation.loop import ModelCall, ModelError, serialized_request
from opspilot.tools.executor import ToolRequest
from opspilot.tools.outcomes import EvidenceRecord, ToolOperation, ToolOutcome, Window
from tests.m1_investigation_support import (
    ScriptedModel,
    assemble,
    reply,
    report_from_transcript,
    tool_call,
)
from tests.test_m1_investigation_client import _FixedOpener

NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)
AUTH = Window(NOW - timedelta(hours=1), NOW)
HASH = sha(b"synthetic")
VERSIONS = {
    name: HASH
    for name in ("tools_schema", "registry", "profile", "projection", "permissions")
}
PARAMS = {"expr": "rate(errors[5m])"}


def context():
    return RecordingContext(
        "source",
        "source-run",
        HASH,
        "synthetic-case",
        "v1",
        True,
        VERSIONS,
        {"question": "synthetic only"},
        {},
        "inputs-0",
        "controls-0",
        NOW,
        NOW + timedelta(seconds=10),
        (
            ToolBinding(
                "metrics.query",
                "v1",
                "target",
                "prometheus",
                encode(AUTH),
                None,
                HASH,
                HASH,
                HASH,
                HASH,
            ),
        ),
    )


def outcome():
    return IncidentOutcome(
        "synthetic-case",
        "completed",
        ("evidence-old",),
        "report_available",
        (),
        ("read_only",),
        None,
        (),
        True,
    )


def first_call(call_id="old-call", params=None, reasoning="source-private"):
    return reply(
        tool_calls=[
            tool_call(
                call_id,
                "metrics.query",
                json.dumps(PARAMS if params is None else params),
            )
        ],
        finish="tool_calls",
        reasoning=reasoning,
    )


def call(messages=None):
    return ModelCall(
        tuple(messages or ({"role": "user", "content": "synthetic only"},)),
        None,
        False,
        20,
        360.0,
    )


def request(step="old-step", params=None):
    return ToolRequest(
        step,
        0,
        "metrics.query",
        "target",
        PARAMS if params is None else params,
        encode(AUTH),
    )


class Executor:
    def __init__(self):
        self.calls = []

    def execute(self, req):
        self.calls.append(req)
        op = ToolOperation(
            req.operation_id,
            "scope",
            "service",
            "target",
            "source-run",
            req.tool_name,
            req.target_ref,
            HASH,
            HASH,
            "metrics.query",
            "v1",
            "prometheus",
            "target",
            window=AUTH,
        )
        view = {
            "evidence_id": "evidence-old",
            "operation_id": op.operation_id,
            "rows": [{"value": 3}],
            "window": encode(AUTH),
        }
        raw = b'{"synthetic":true}'
        evidence = EvidenceRecord(
            "evidence-old",
            op,
            "ok",
            raw,
            sha(raw),
            view,
            sha(canonical_bytes(view)),
            HASH,
            NOW,
            NOW,
            AUTH.start,
            AUTH.end,
            1,
            False,
            False,
            0,
            0,
            True,
        )
        return ToolOutcome(op, "ok", None, "confirmed", view, evidence)


def conversation(model, tools, delivery, *, step="old-step", params=None, stub=True):
    initial = call()
    response = model.complete(initial)
    req = request(step, params)
    result = tools.execute(req)
    visible = (
        {"evidence_id": "evidence-old", "stub": True} if stub else result.model_view
    )
    message = {
        "role": "tool",
        "tool_call_id": response.tool_calls[0]["id"],
        "content": canonical_bytes(visible).decode(),
    }
    delivery(result.model_view, message)
    assistant = {
        "role": "assistant",
        "content": response.content,
        "reasoning_content": response.reasoning_content,
        "tool_calls": list(response.tool_calls),
    }
    model.complete(call([*initial.messages, assistant, message]))
    return outcome()


def source(tmp_path, *, driver=None, ctx=None):
    destination = tmp_path / "source"
    if driver is None:

        def driver(hooks):
            return conversation(
                hooks.model(ScriptedModel([first_call(), reply(content="old report")])),
                hooks.tools(Executor()),
                hooks.delivery,
            )

    ref = record(driver, destination=destination, context=ctx or context())
    return load_recording(destination / ref.path, expected_hash=ref.sha256)


def manifest_attempt(recording, mode="replay"):
    dimensions = {
        name: DimensionValue("hash", HASH, None) for name in REQUIRED_DIMENSIONS
    }
    dimensions["replay_fixture"] = DimensionValue("hash", recording.ref.sha256, None)
    manifest = Manifest("round-new", dimensions)
    attempt = AttemptRecord(
        "attempt-new",
        manifest.round_id,
        manifest_hash(manifest),
        "synthetic-case",
        "v1",
        mode,
        1,
        True,
        "ran",
        False,
        "run-new",
    )
    return manifest, attempt


def run_replay(
    recording, tmp_path, *, model=None, driver=None, manifest=None, attempt=None
):
    default_manifest, default_attempt = manifest_attempt(recording)
    return replay(
        recording,
        mode="replay",
        manifest=manifest or default_manifest,
        attempt=attempt or default_attempt,
        investigator=model
        or ScriptedModel(
            [
                first_call("new-call", reasoning="new-private"),
                reply(content="new report"),
            ]
        ),
        driver=driver
        or (
            lambda session: conversation(
                session.model, session.tools, session.capture_delivery, step="new-step"
            )
        ),
        destination=tmp_path / "new-recording",
    )


def rewrite(recording, mutate):
    data = json.loads(recording.path.read_bytes())
    mutate(data)
    raw = canonical_bytes(data)
    recording.path.write_bytes(raw)
    return load_recording(recording.path, expected_hash=sha(raw))


def test_complete_synthetic_packet_separates_raw_view_and_stub(tmp_path):
    recording = source(tmp_path)
    assert recording.complete and recording.context.synthetic
    assert recording.context.source_versions == VERSIONS
    assert recording.context.clock_policy == "event-clock/frozen-end-real-monotonic"
    assert len(recording.context.input_refs) == 1
    assert json_blob(recording, recording.context.input_refs[0]) == dict(
        recording.context.stimulus
    )
    assert [event.kind for event in recording.events] == [
        "model",
        "tool",
        "delivery",
        "model",
    ]
    delivery = recording.events[2]
    view = json_blob(recording, delivery.request_ref)
    message = json_blob(recording, delivery.response_ref)
    assert view["rows"] == [{"value": 3}]
    assert "rows" not in json.loads(message["content"])
    assert delivery.hashes["view_sha256"] != delivery.hashes["delivery_sha256"]
    model = json_blob(recording, recording.events[0].request_ref)
    assert model["body"].encode() == serialized_request(call())
    assert (recording.path.parent.stat().st_mode & 0o777) == 0o700
    assert not (recording.path.parent / "INCOMPLETE").exists()


def test_structural_uses_no_client_or_environment(tmp_path, monkeypatch):
    recording = source(tmp_path)
    manifest, attempt = manifest_attempt(recording, "structural")

    class Forbidden:
        def complete(self, call):
            raise AssertionError("client accessed")

    def forbidden(*args, **kwargs):
        raise AssertionError("credentials accessed")

    monkeypatch.setattr("os.getenv", forbidden)
    result = replay(
        recording,
        mode="structural",
        attempt=attempt,
        manifest=manifest,
        investigator=Forbidden(),
        driver=forbidden,
    )
    assert result.structural_verified and result.recording_ref is None
    assert result.outcome is None and result.error is None


def test_current_model_calls_and_frozen_evidence_preserve_source_identity(tmp_path):
    recording = source(tmp_path)
    model = ScriptedModel(
        [
            first_call("new-call", reasoning="fresh-private"),
            reply(content="different new report"),
        ]
    )
    result = run_replay(recording, tmp_path, model=model)
    assert result.error is None, result
    assert len(model.calls) == 2
    assert all(
        "source-private" not in serialized_request(item).decode()
        for item in model.calls
    )
    assert "fresh-private" in serialized_request(model.calls[1]).decode()
    assert result.id_map == {"new-step-t0": "old-step-t0"}
    assert result.source_run_id == "source-run" and result.replay_run_id == "run-new"
    assert result.business_time == recording.context.ended_at
    reread = load_recording(
        tmp_path / "new-recording" / "recording.json",
        expected_hash=result.recording_ref.sha256,
    )
    old_view = json_blob(recording, recording.events[1].response_ref)["model_view"]
    new_view = json_blob(reread, reread.events[1].response_ref)["model_view"]
    assert old_view == new_view
    assert (
        json_blob(reread, reread.events[-1].response_ref)["content"]
        == "different new report"
    )


@pytest.mark.parametrize(
    "params",
    [
        {"expr": "rate(errors[5m]) "},
        {"expr": "other"},
        {"expr": "rate(errors[5m])", "extra": None},
        {"expr": 1},
        {"expr": True},
    ],
)
def test_exact_tool_miss_stops_and_counts_attempt(tmp_path, params):
    recording = source(tmp_path)
    model = ScriptedModel(
        [first_call("new-call", params=params), reply(content="must not be called")]
    )
    result = run_replay(
        recording,
        tmp_path,
        model=model,
        driver=lambda session: conversation(
            session.model,
            session.tools,
            session.capture_delivery,
            step="new",
            params=params,
        ),
    )
    assert result.error == "REPLAY_MISS" and result.attempt.replay_miss
    assert result.attempt.started and result.attempt.status == "aborted"
    assert len(model.calls) == 1
    assert result.recording_ref is not None
    with pytest.raises(RecordingError, match="RECORDING_INCOMPLETE"):
        load_recording(
            tmp_path / "new-recording" / "recording.json",
            expected_hash=result.recording_ref.sha256,
        )


def test_exhaustion_is_sticky_even_if_driver_catches_error(tmp_path):
    recording = source(tmp_path)

    def drive(session):
        session.model.complete(call())
        req = request("new")
        session.tools.execute(req)
        with pytest.raises(RecordingError, match="REPLAY_MISS"):
            session.tools.execute(req)
        with pytest.raises(RecordingError, match="REPLAY_MISS"):
            session.model.complete(call())
        return outcome()

    result = run_replay(recording, tmp_path, driver=drive)
    assert result.error == "REPLAY_MISS" and result.attempt.replay_miss


def test_model_can_end_early_without_using_tool_tail(tmp_path):
    recording = source(tmp_path)
    model = ScriptedModel([reply(content="early stop")])
    result = run_replay(
        recording,
        tmp_path,
        model=model,
        driver=lambda session: (session.model.complete(call()), outcome())[1],
    )
    assert result.error is None and len(model.calls) == 1 and not result.id_map


def test_manifest_prompt_and_model_can_change_but_tools_cannot(tmp_path):
    recording = source(tmp_path)
    manifest, attempt = manifest_attempt(recording)
    dimensions = dict(manifest.dimensions)
    dimensions["prompt"] = DimensionValue("hash", sha(b"new prompt"), None)
    dimensions["model_request"] = DimensionValue("hash", sha(b"new model"), None)
    changed = replace(manifest, dimensions=dimensions)
    changed_attempt = replace(attempt, manifest_hash=manifest_hash(changed))
    result = run_replay(recording, tmp_path, manifest=changed, attempt=changed_attempt)
    assert result.error is None


@pytest.mark.parametrize(
    "dimension", ["tools_schema", "registry", "profile", "projection", "permissions"]
)
def test_incompatible_manifest_is_miss_before_model(tmp_path, dimension):
    recording = source(tmp_path)
    manifest, attempt = manifest_attempt(recording)
    dimensions = dict(manifest.dimensions)
    dimensions[dimension] = DimensionValue("hash", sha(b"different"), None)
    manifest = replace(manifest, dimensions=dimensions)
    attempt = replace(attempt, manifest_hash=manifest_hash(manifest))
    model = ScriptedModel([])
    result = run_replay(
        recording, tmp_path, manifest=manifest, attempt=attempt, model=model
    )
    assert result.error == "REPLAY_MISS" and not model.calls


def test_cross_run_private_reasoning_rejected_before_client(tmp_path):
    recording = source(tmp_path)
    model = ScriptedModel([])
    result = run_replay(
        recording,
        tmp_path,
        model=model,
        driver=lambda session: (
            session.model.complete(
                call(
                    [
                        {
                            "role": "assistant",
                            "content": None,
                            "reasoning_content": "source-private",
                            "tool_calls": [],
                        }
                    ]
                )
            ),
            outcome(),
        )[1],
    )
    assert result.error == "IDENTITY_MISMATCH" and not model.calls


def test_frozen_business_clock_real_monotonic_and_timeout_clamp(tmp_path):
    recording = source(tmp_path)
    model = ScriptedModel([reply(content="done")])

    def drive(session):
        assert session.clock.now() == recording.context.ended_at
        assert session.clock.monotonic() > 0
        session.model.complete(replace(call(), timeout_seconds=9999))
        return outcome()

    result = run_replay(recording, tmp_path, model=model, driver=drive)
    assert result.error is None and model.calls[0].timeout_seconds == 360.0


def test_timeout_is_real_monotonic_and_sticky(tmp_path, monkeypatch):
    import importlib

    replay_module = importlib.import_module("opspilot.evaluation.replay")
    recording = source(tmp_path)
    elapsed = [10.0]
    monkeypatch.setattr(replay_module.time, "monotonic", lambda: elapsed[0])

    class Slow:
        def complete(self, request):
            elapsed[0] += 361
            return reply(content="late")

    result = run_replay(
        recording,
        tmp_path,
        model=Slow(),
        driver=lambda session: (session.model.complete(call()), outcome())[1],
    )
    assert result.error == "TIMEOUT" and result.attempt.status == "aborted"


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda obj: obj.update(extra=True), "SCHEMA_INVALID"),
        (lambda obj: obj.update(schema="future"), "SCHEMA_INVALID"),
        (lambda obj: obj.update(complete="false"), "SCHEMA_INVALID"),
        (lambda obj: obj.update(complete=False), "RECORDING_INCOMPLETE"),
        (lambda obj: obj["events"][0].update(seq=2), "IDENTITY_MISMATCH"),
        (
            lambda obj: obj["events"][0].update(identity="other-run"),
            "IDENTITY_MISMATCH",
        ),
        (
            lambda obj: obj["events"][0]["hashes"].update(request_body_sha256="0" * 64),
            "HASH_MISMATCH",
        ),
        (
            lambda obj: obj["events"][0]["request_ref"].update(path="../escape"),
            "SCHEMA_INVALID",
        ),
        (lambda obj: obj["events"][0]["request_ref"].update(bytes=0), "HASH_MISMATCH"),
        (lambda obj: obj["events"][2].update(parent=0), "IDENTITY_MISMATCH"),
    ],
)
def test_strict_loader_rejects_self_resigned_metadata(tmp_path, mutation, code):
    recording = source(tmp_path)
    with pytest.raises(RecordingError, match=code):
        rewrite(recording, mutation)


def test_blob_tampering_and_symlink_are_not_followed(tmp_path):
    recording = source(tmp_path)
    artifact = recording.events[0].request_ref
    path = recording.path.parent / artifact.path
    original = path.read_bytes()
    path.write_bytes(original + b" ")
    with pytest.raises(RecordingError, match="HASH_MISMATCH"):
        load_recording(recording.path, expected_hash=recording.ref.sha256)
    external = tmp_path / "external"
    external.write_bytes(original)
    path.unlink()
    path.symlink_to(external)
    with pytest.raises(RecordingError, match="EVIDENCE_UNAVAILABLE"):
        load_recording(recording.path, expected_hash=recording.ref.sha256)


def test_duplicate_json_keys_and_outer_hash_rejected(tmp_path):
    recording = source(tmp_path)
    with pytest.raises(RecordingError, match="HASH_MISMATCH"):
        load_recording(recording.path, expected_hash="0" * 64)
    raw = b'{"schema":"opspilot-replay-1","schema":"opspilot-replay-1"}'
    recording.path.write_bytes(raw)
    with pytest.raises(RecordingError, match="SCHEMA_INVALID"):
        load_recording(recording.path, expected_hash=sha(raw))


def test_control_events_fail_closed_and_preserve_incomplete(tmp_path):
    with pytest.raises(RecordingError, match="SCHEMA_INVALID"):
        source(tmp_path, driver=lambda hooks: hooks.control({"kind": "pause"}))
    path = tmp_path / "source" / "recording.json"
    obj = json.loads(path.read_bytes())
    assert obj["complete"] is False and obj["incomplete_reasons"] == ["SCHEMA_INVALID"]
    assert (path.parent / "INCOMPLETE").exists()


def test_driver_error_sanitized_and_never_overwrites(tmp_path):
    def fail(hooks):
        raise RuntimeError("Bearer secret-do-not-record")

    with pytest.raises(RecordingError, match="RECORDING_INCOMPLETE") as caught:
        source(tmp_path, driver=fail)
    assert caught.value.__cause__ is None
    raw = (tmp_path / "source" / "recording.json").read_bytes()
    assert b"secret-do-not-record" not in raw
    with pytest.raises(RecordingError, match="OUTPUT_EXISTS"):
        source(tmp_path)


def test_model_oversize_is_incomplete_without_truncated_blob(tmp_path):
    from opspilot.evaluation.recording import MAX_RESPONSE_BYTES

    with pytest.raises(RecordingError, match="RECORDING_INCOMPLETE"):
        source(
            tmp_path,
            driver=lambda hooks: (
                hooks.model(
                    ScriptedModel([reply(content="x" * MAX_RESPONSE_BYTES)])
                ).complete(call()),
                outcome(),
            )[1],
        )
    metadata = json.loads((tmp_path / "source" / "recording.json").read_bytes())
    assert metadata["complete"] is False
    assert metadata["events"][0]["response_ref"] is None


def test_failed_physical_request_and_retry_both_recorded(tmp_path):
    model = ScriptedModel([ModelError("MODEL_UNAVAILABLE"), reply(content="retried")])

    def drive(hooks):
        client = hooks.model(model)
        with pytest.raises(ModelError):
            client.complete(call())
        client.complete(call())
        return outcome()

    recording = source(tmp_path, driver=drive)
    assert [event.error for event in recording.events] == ["MODEL_UNAVAILABLE", None]
    assert recording.events[0].request_ref == recording.events[1].request_ref


def test_http_hook_records_raw_without_auth_header_and_default_bytes_identical(
    tmp_path,
):
    payload = canonical_bytes(
        {
            "id": "synthetic-response",
            "model": "deepseek-flash",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "done",
                        "reasoning_content": "private",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {},
        }
    )
    requests = []

    class Opener(_FixedOpener):
        def open(self, req, timeout):
            requests.append((req.data, timeout))
            assert req.get_header("Authorization") == "Bearer synthetic-key"
            return super().open(req, timeout)

    plain = DeepSeekClient("synthetic-key", opener=Opener(payload)).complete(call())

    def drive(hooks):
        client = DeepSeekClient(
            "synthetic-key", opener=Opener(payload), capture_http=hooks.http
        )
        captured = hooks.model(client).complete(call())
        assert captured == plain
        return outcome()

    recording = source(tmp_path, driver=drive)
    assert requests == [(serialized_request(call()), 360.0)] * 2
    assert [event.kind for event in recording.events] == ["model", "http"]
    http = recording.events[1]
    raw = (recording.path.parent / http.response_ref.path).read_bytes()
    assert raw == payload
    assert b"synthetic-key" not in b"".join(
        item.read_bytes() for item in (recording.path.parent / "blobs").iterdir()
    )


def test_http_error_body_kept_only_when_capture_enabled(tmp_path):
    class ErrorOpener:
        def open(self, request, timeout):
            raise HTTPError(
                "https://synthetic.invalid",
                503,
                "synthetic",
                {},
                io.BytesIO(b'{"error":"synthetic"}'),
            )

    assert DeepSeekClient("synthetic", opener=ErrorOpener())._post(b"{}", 10) == (
        b"",
        503,
    )

    def drive(hooks):
        client = hooks.model(
            DeepSeekClient("synthetic", opener=ErrorOpener(), capture_http=hooks.http)
        )
        with pytest.raises(ModelError, match="MODEL_UNAVAILABLE"):
            client.complete(call())
        return outcome()

    recording = source(tmp_path, driver=drive)
    assert recording.events[0].error == "MODEL_UNAVAILABLE"
    http = recording.events[1]
    assert (
        recording.path.parent / http.response_ref.path
    ).read_bytes() == b'{"error":"synthetic"}'


def test_loop_delivery_hook_preserves_default_request_bytes_and_hashes(monkeypatch):
    from uuid import UUID

    monkeypatch.setattr("opspilot.investigation.loop.uuid4", lambda: UUID(int=7))
    monkeypatch.setattr("opspilot.tools.executor.uuid4", lambda: UUID(int=8))
    monkeypatch.setattr("opspilot.investigation.store.uuid4", lambda: UUID(int=9))

    def run(enabled):
        loop, request, model, _, _, _ = assemble(
            replies=[
                reply(tool_calls=[tool_call()], finish="tool_calls"),
                report_from_transcript,
            ],
            model_requests=2,
        )
        deliveries = []
        if enabled:
            loop.capture_delivery = lambda view, message: deliveries.append(
                (view, message)
            )
        result = loop.run(request)
        return result, [serialized_request(item) for item in model.calls], deliveries

    plain, plain_requests, _ = run(False)
    captured, captured_requests, deliveries = run(True)
    # Operation/step UUIDs are stable in the test MemoryStepStore.
    assert captured == plain
    assert captured_requests == plain_requests
    assert [hashlib.sha256(item).hexdigest() for item in captured_requests] == [
        hashlib.sha256(item).hexdigest() for item in plain_requests
    ]
    assert len(deliveries) == 1
    assert deliveries[0][1] in json.loads(captured_requests[1])["messages"]


def test_occurrences_consumed_in_order_and_unused_tail_allowed(tmp_path):
    def drive(hooks):
        model = hooks.model(
            ScriptedModel(
                [
                    first_call("one"),
                    reply(content="one"),
                    first_call("two"),
                    reply(content="two"),
                ]
            )
        )
        tools = hooks.tools(Executor())
        conversation(model, tools, hooks.delivery, step="first-old")
        return conversation(model, tools, hooks.delivery, step="second-old")

    recording = source(tmp_path, driver=drive)
    occurrences = [
        json_blob(recording, event.request_ref)["occurrence"]
        for event in recording.events
        if event.kind == "tool"
    ]
    assert occurrences == [0, 1]
    model = ScriptedModel(
        [
            first_call("new-one", reasoning="fresh"),
            reply(content="done one"),
            first_call("new-two", reasoning="fresh"),
            reply(content="done two"),
        ]
    )

    def replay_drive(session):
        conversation(
            session.model, session.tools, session.capture_delivery, step="new-one"
        )
        return conversation(
            session.model, session.tools, session.capture_delivery, step="new-two"
        )

    result = run_replay(recording, tmp_path, model=model, driver=replay_drive)
    assert result.error is None
    assert result.id_map == {
        "new-one-t0": "first-old-t0",
        "new-two-t0": "second-old-t0",
    }


def test_missing_delivery_and_invented_tool_dispatch_refused(tmp_path):
    def missing(hooks):
        hooks.model(ScriptedModel([first_call()])).complete(call())
        hooks.tools(Executor()).execute(request())
        return outcome()

    with pytest.raises(RecordingError, match="RECORDING_INCOMPLETE"):
        source(tmp_path, driver=missing)


def test_unknown_private_field_is_rejected_before_current_model(tmp_path):
    recording = source(tmp_path)
    model = ScriptedModel([])
    result = run_replay(
        recording,
        tmp_path,
        model=model,
        driver=lambda session: (
            session.model.complete(
                call(
                    [
                        {
                            "role": "assistant",
                            "content": None,
                            "reasoning_details": "old-private",
                        }
                    ]
                )
            ),
            outcome(),
        )[1],
    )
    assert result.error == "SCHEMA_INVALID" and not model.calls


def test_source_private_field_cannot_hide_in_initial_state(tmp_path):
    with pytest.raises(RecordingError, match="SCHEMA_INVALID"):
        source(
            tmp_path,
            ctx=replace(
                context(),
                initial_state={"history": [{"reasoning_content": "old-private"}]},
            ),
        )
    assert not (tmp_path / "source").exists()


def test_real_run_wall_limit_expires_even_without_model_call(tmp_path, monkeypatch):
    import importlib

    replay_module = importlib.import_module("opspilot.evaluation.replay")
    recording = source(tmp_path)
    elapsed = [10.0]
    monkeypatch.setattr(replay_module.time, "monotonic", lambda: elapsed[0])

    def drive(session):
        elapsed[0] += 7201
        return outcome()

    result = run_replay(recording, tmp_path, driver=drive)
    assert result.error == "TIMEOUT" and result.attempt.status == "aborted"


def test_finish_seals_final_business_clock(tmp_path):
    def drive(hooks):
        hooks.model(ScriptedModel([reply(content="done")])).complete(call())
        hooks.finish(NOW + timedelta(seconds=1))
        return outcome()

    recording = source(tmp_path, driver=drive)
    assert recording.context.ended_at == NOW + timedelta(seconds=1)


def test_unmodified_investigation_loop_can_record_and_replay_frozen_tools(tmp_path):
    from types import SimpleNamespace

    from opspilot.investigation.store import MemoryStepStore

    loop, req, _, transport, _, _ = assemble(
        replies=[
            reply(tool_calls=[tool_call()], finish="tool_calls"),
            report_from_transcript,
        ],
        model_requests=2,
    )
    ctx = replace(
        context(),
        source_run_id=req.run_id,
        started_at=loop.clock.now(),
        ended_at=loop.clock.now() + timedelta(seconds=10),
        bindings=(
            ToolBinding(
                "metrics.range_query",
                "v1",
                "checkout-prod",
                "prometheus",
                encode(req.scope.window),
                None,
                HASH,
                HASH,
                HASH,
                HASH,
            ),
        ),
    )

    def project(result):
        return replace(
            outcome(),
            final_state=result.execution,
            evidence_ids=result.evidence_ids,
            report_available=result.report is not None,
        )

    def capture_drive(hooks):
        loop.model = hooks.model(loop.model)
        loop.executor = hooks.tools(loop.executor)
        loop.capture_delivery = hooks.delivery
        return project(loop.run(req))

    recording = source(tmp_path, driver=capture_drive, ctx=ctx)
    assert transport.called
    model = ScriptedModel(
        [
            reply(
                tool_calls=[tool_call("fresh-call")],
                finish="tool_calls",
                reasoning="fresh-private",
            ),
            report_from_transcript,
        ]
    )

    def drive(session):
        fresh, _, _, live_transport, _, _ = assemble(replies=[], model_requests=2)
        scope = replace(req.scope, run_id="run-new")
        fresh.clock = session.clock
        fresh.store = MemoryStepStore(
            budget_limit=4,
            deadline=scope.deadline,
            clock=session.clock,
            run_id="run-new",
        )
        fresh.executor = SimpleNamespace(
            scope=scope, tool_seconds_used=0.0, execute=session.tools.execute
        )
        fresh.model = session.model
        fresh.capture_delivery = session.capture_delivery
        request = replace(
            req,
            run_id="run-new",
            scope=scope,
            evidence_context={**req.evidence_context, "run_id": "run-new"},
        )
        result = project(fresh.run(request))
        assert not live_transport.called
        return result

    result = run_replay(recording, tmp_path, model=model, driver=drive)
    assert result.error is None, result
    assert result.outcome.final_state == "completed"
    assert result.outcome.evidence_ids == recording.outcome.evidence_ids
    assert len(model.calls) == 2


def test_unknown_binding_miss_is_sticky_when_driver_catches_it(tmp_path):
    recording = source(tmp_path)
    model = ScriptedModel([first_call("new-call")])

    def drive(session):
        session.model.complete(call())
        with pytest.raises(RecordingError, match="REPLAY_MISS"):
            session.tools.execute(replace(request("new"), target_ref="unfrozen"))
        return outcome()

    result = run_replay(recording, tmp_path, model=model, driver=drive)
    assert result.error == "REPLAY_MISS" and result.attempt.replay_miss


def test_crashed_recording_marker_rejects_even_without_index(tmp_path):
    destination = tmp_path / "crashed"
    destination.mkdir()
    (destination / "INCOMPLETE").write_bytes(b"RECORDING_INCOMPLETE")
    with pytest.raises(RecordingError, match="RECORDING_INCOMPLETE"):
        load_recording(destination / "recording.json", expected_hash=HASH)


def test_transport_oversize_cannot_be_complete_even_if_driver_catches(tmp_path):
    def drive(hooks):
        model = hooks.model(ScriptedModel([ModelError("RESPONSE_TOO_LARGE")]))
        with pytest.raises(ModelError):
            model.complete(call())
        return outcome()

    with pytest.raises(RecordingError, match="RECORDING_INCOMPLETE"):
        source(tmp_path, driver=drive)


@pytest.mark.parametrize("declared", [True, False])
def test_trailing_delivery_still_requires_a_declared_model_call(tmp_path, declared):
    def drive(hooks):
        last = first_call("late") if declared else reply(content="stop")
        model = hooks.model(ScriptedModel([first_call(), last]))
        views = []

        def deliver(view, message):
            views.append(view)
            hooks.delivery(view, message)

        conversation(model, hooks.tools(Executor()), deliver)
        hooks.delivery(
            views[0],
            {
                "role": "tool",
                "tool_call_id": "late",
                "content": canonical_bytes(views[0]).decode(),
            },
        )
        return outcome()

    if not declared:
        with pytest.raises(RecordingError, match="IDENTITY_MISMATCH"):
            source(tmp_path, driver=drive)
    else:
        recording = source(tmp_path, driver=drive)
        assert recording.events[-1].kind == "delivery"
        manifest, attempt = manifest_attempt(recording, "structural")
        assert replay(
            recording,
            mode="structural",
            attempt=attempt,
            manifest=manifest,
            investigator=None,
        ).structural_verified


def test_structural_null_run_accepts_version_fixture_but_rejects_wrong_mode(tmp_path):
    recording = source(tmp_path)
    manifest, attempt = manifest_attempt(recording, "structural")
    dimensions = dict(manifest.dimensions)
    dimensions["replay_fixture"] = DimensionValue("version", "v1", None)
    manifest = replace(manifest, dimensions=dimensions)
    attempt = replace(
        attempt, manifest_hash=manifest_hash(manifest), product_run_id=None
    )
    capture = replay(
        recording,
        mode="structural",
        attempt=attempt,
        manifest=manifest,
        investigator=None,
    )
    assert capture.replay_run_id is None and capture.structural_verified
    with pytest.raises(RecordingError, match="IDENTITY_MISMATCH"):
        replay(
            recording,
            mode="structural",
            attempt=replace(attempt, mode="replay"),
            manifest=manifest,
            investigator=None,
        )


def _contradict_tool(result, field):
    operation = result.operation
    view = dict(result.model_view)
    if field in ("requested_tool", "requested_target"):
        operation = replace(operation, **{field: "different"})
    elif field == "view_operation":
        view["operation_id"] = "other-operation"
    else:
        operation = replace(
            operation, window=Window(AUTH.start + timedelta(minutes=1), AUTH.end)
        )
    evidence = replace(
        result.evidence,
        operation=operation,
        _view=view,
        view_sha256=sha(canonical_bytes(view)),
    )
    return replace(result, operation=operation, model_view=view, evidence=evidence)


@pytest.mark.parametrize(
    "field",
    [
        "requested_tool",
        "requested_target",
        "view_operation",
        "explicit_window",
        "default_window",
    ],
)
def test_contradictory_tool_associations_cannot_record_complete(tmp_path, field):
    class Contradictory(Executor):
        def execute(self, req):
            return _contradict_tool(super().execute(req), field)

    def drive(hooks):
        # A default lookup uses the whole authorized window in this fixture.
        params = request("old-step")
        if field == "default_window":
            params = replace(params, window=None)
        hooks.model(ScriptedModel([first_call()])).complete(call())
        hooks.tools(Contradictory()).execute(params)
        return outcome()

    with pytest.raises(RecordingError, match="IDENTITY_MISMATCH"):
        source(tmp_path, driver=drive)
    assert (
        json.loads((tmp_path / "source" / "recording.json").read_bytes())["complete"]
        is False
    )


@pytest.mark.parametrize(
    "field", ["requested_tool", "requested_target", "view_operation", "window"]
)
def test_resigned_contradictory_packet_is_rejected_before_model(tmp_path, field):
    from opspilot.evaluation.recording import unwire, wire

    recording = source(tmp_path)
    event = recording.events[1]
    payload = json_blob(recording, event.response_ref)
    result = _contradict_tool(unwire(ToolOutcome, payload), field)
    data = canonical_bytes(wire(result))
    ref = replace(
        event.response_ref, path="blobs/" + sha(data), sha256=sha(data), bytes=len(data)
    )
    (recording.path.parent / ref.path).write_bytes(data)
    meta = json.loads(recording.path.read_bytes())
    meta["events"][1]["response_ref"] = encode(ref)
    meta["events"][1]["hashes"]["view_sha256"] = sha(canonical_bytes(result.model_view))
    raw = canonical_bytes(meta)
    recording.path.write_bytes(raw)
    with pytest.raises(RecordingError, match="IDENTITY_MISMATCH"):
        load_recording(recording.path, expected_hash=sha(raw))
    candidate = replace(
        recording, ref=replace(recording.ref, sha256=sha(raw), bytes=len(raw))
    )
    model = ScriptedModel([])
    with pytest.raises(RecordingError, match="IDENTITY_MISMATCH"):
        run_replay(candidate, tmp_path, model=model)
    assert model.calls == []


@pytest.mark.parametrize(
    "error", ["incomplete_read", "io_error", "timeout", "short_content_length"]
)
def test_error_body_truncation_is_incomplete_with_default_result_preserved(
    tmp_path, error
):
    from http.client import IncompleteRead

    class Body:
        def read(self, size=-1):
            if error == "incomplete_read":
                raise IncompleteRead(b"partial")
            if error == "timeout":
                raise TimeoutError("synthetic timeout")
            if error == "short_content_length":
                return b"partial"
            raise OSError("synthetic error")

        def close(self):
            pass

    class Opener:
        def open(self, req, timeout):
            raise HTTPError(
                "https://synthetic.invalid",
                503,
                "synthetic",
                {"Content-Length": "99"} if error == "short_content_length" else {},
                Body(),
            )

    assert DeepSeekClient("synthetic", opener=Opener())._post(b"{}", 5.0) == (b"", 503)
    for capture in (None, lambda *args: None):
        with pytest.raises(ModelError, match="MODEL_UNAVAILABLE"):
            DeepSeekClient("synthetic", opener=Opener(), capture_http=capture).complete(
                call()
            )

    def drive(hooks):
        model = hooks.model(
            DeepSeekClient("synthetic", opener=Opener(), capture_http=hooks.http)
        )
        with pytest.raises(ModelError, match="MODEL_UNAVAILABLE"):
            model.complete(call())
        return outcome()

    with pytest.raises(RecordingError, match="RECORDING_INCOMPLETE"):
        source(tmp_path, driver=drive)
    path = tmp_path / "source" / "recording.json"
    meta = json.loads(path.read_bytes())
    assert "HTTP_BODY_INCOMPLETE" in meta["incomplete_reasons"]
    http = next(item for item in meta["events"] if item["kind"] == "http")
    assert http["hashes"]["status"] == "503" and http["response_ref"] is None
    assert http["error"] == "HTTP_BODY_INCOMPLETE"
    # The reader must reject a known truncation even if the outer flags lie.
    (path.parent / "INCOMPLETE").unlink()
    meta.update(complete=True, incomplete_reasons=[])
    raw = canonical_bytes(meta)
    path.write_bytes(raw)
    with pytest.raises(RecordingError, match="RECORDING_INCOMPLETE"):
        load_recording(path, expected_hash=sha(raw))


def test_loop_duplicate_refusal_records_and_replays_without_facade_call(tmp_path):
    from types import SimpleNamespace

    from opspilot.investigation.store import MemoryStepStore

    replies = [
        reply(tool_calls=[tool_call("one")], finish="tool_calls"),
        reply(tool_calls=[tool_call("two")], finish="tool_calls"),
        report_from_transcript,
    ]
    loop, req, _, transport, _, _ = assemble(replies=replies, model_requests=3)
    ctx = replace(
        context(),
        source_run_id=req.run_id,
        started_at=loop.clock.now(),
        ended_at=loop.clock.now(),
        bindings=(
            ToolBinding(
                "metrics.range_query",
                "v1",
                "checkout-prod",
                "prometheus",
                encode(req.scope.window),
                None,
                HASH,
                HASH,
                HASH,
                HASH,
            ),
        ),
    )
    counts = []
    executor = loop.executor

    def execute(req):
        counts.append(req)
        return executor.execute(req)

    def drive(hooks):
        loop.model = hooks.model(loop.model)
        loop.executor = hooks.tools(
            SimpleNamespace(
                scope=executor.scope, tool_seconds_used=0.0, execute=execute
            )
        )
        loop.capture_delivery = hooks.delivery
        assert loop.run(req).execution == "completed"
        return outcome()

    recording = source(tmp_path, driver=drive, ctx=ctx)
    assert transport.called and len(counts) == 1
    refusals = [event for event in recording.events if event.kind == "refusal"]
    assert len(refusals) == 1
    refused = json_blob(recording, refusals[0].response_ref)
    original = json_blob(recording, recording.events[1].response_ref)["model_view"]
    assert refused["reason"] == "DUPLICATE_TOOL_CALL"
    assert refused["operation_id"] != original["operation_id"]
    assert refused["evidence_id"] == original["evidence_id"]
    manifest, attempt = manifest_attempt(recording, "structural")
    assert replay(
        recording,
        mode="structural",
        attempt=attempt,
        manifest=manifest,
        investigator=None,
    ).structural_verified
    fresh_model = ScriptedModel(
        [
            reply(tool_calls=[tool_call("fresh-one")], finish="tool_calls"),
            reply(tool_calls=[tool_call("fresh-two")], finish="tool_calls"),
            report_from_transcript,
        ]
    )

    def replay_drive(session):
        fresh, _, _, unused_transport, _, _ = assemble(replies=[], model_requests=3)
        scope = replace(req.scope, run_id="run-new")
        calls = []

        def frozen_execute(request):
            calls.append(request)
            return session.tools.execute(request)

        fresh.clock = session.clock
        fresh.store = MemoryStepStore(
            budget_limit=4,
            deadline=scope.deadline,
            clock=session.clock,
            run_id="run-new",
        )
        fresh.executor = SimpleNamespace(
            scope=scope, tool_seconds_used=0.0, execute=frozen_execute
        )
        fresh.model = session.model
        fresh.capture_delivery = session.capture_delivery
        result = fresh.run(
            replace(
                req,
                run_id="run-new",
                scope=scope,
                evidence_context={**req.evidence_context, "run_id": "run-new"},
            )
        )
        assert result.execution == "completed"
        assert len(calls) == 1 and not unused_transport.called
        return outcome()

    capture = run_replay(recording, tmp_path, model=fresh_model, driver=replay_drive)
    assert capture.error is None, capture
    assert len(capture.id_map) == 2  # The real tool and the separate refusal.
    assert len(fresh_model.calls) == 3
    copied = load_recording(
        tmp_path / "new-recording" / "recording.json",
        expected_hash=capture.recording_ref.sha256,
    )
    new_refusal = next(event for event in copied.events if event.kind == "refusal")
    current = json_blob(copied, new_refusal.response_ref)
    assert current["operation_id"] in capture.id_map
    assert capture.id_map[current["operation_id"]] == refused["operation_id"]
    assert {**current, "operation_id": refused["operation_id"]} == refused


def test_unrecorded_loop_refusal_is_sticky_replay_miss(tmp_path):
    from opspilot.investigation.loop import _duplicate_call_view

    recording = source(tmp_path)
    model = ScriptedModel(
        [
            first_call("first"),
            first_call("unrecorded"),
            reply(content="must not be called"),
        ]
    )

    def drive(session):
        conversation(
            session.model, session.tools, session.capture_delivery, step="new-tool"
        )
        denied = _duplicate_call_view("new-refusal", evidence_id="evidence-old")
        with pytest.raises(RecordingError, match="REPLAY_MISS"):
            session.capture_delivery(
                denied,
                {
                    "role": "tool",
                    "tool_call_id": "unrecorded",
                    "content": canonical_bytes(denied).decode(),
                },
            )
        with pytest.raises(RecordingError, match="REPLAY_MISS"):
            session.model.complete(call())
        return outcome()

    capture = run_replay(recording, tmp_path, model=model, driver=drive)
    assert capture.error == "REPLAY_MISS" and capture.attempt.replay_miss
    assert len(model.calls) == 2
