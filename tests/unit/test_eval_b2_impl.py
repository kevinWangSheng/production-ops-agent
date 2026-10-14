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
    ToolSnapshot,
    bind_tool_snapshots,
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
            dispatched=True,
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
    dimensions.update(
        {
            name: DimensionValue("hash", value, None)
            for name, value in recording.context.versions.items()
        }
    )
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


@pytest.mark.parametrize("mixed", [False, True])
def test_loop_duplicate_refusal_records_and_replays_without_facade_call(
    tmp_path, mixed
):
    from types import SimpleNamespace

    from opspilot.investigation.store import MemoryStepStore

    first = [tool_call("one")]
    repeated = tool_call("two")
    if mixed:
        first = [
            tool_call("a", arguments='{"expr":"up"}'),
            tool_call("a-duplicate", arguments='{"expr":"up"}'),
            tool_call("b", arguments='{"expr":"down"}'),
        ]
        repeated = tool_call("b-duplicate", arguments='{"expr":"down"}')
    expected_calls, expected_refusals = (2, 2) if mixed else (1, 1)
    replies = [
        reply(tool_calls=first, finish="tool_calls"),
        reply(tool_calls=[repeated], finish="tool_calls"),
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
    assert transport.called and len(counts) == expected_calls
    refusals = [event for event in recording.events if event.kind == "refusal"]
    assert len(refusals) == expected_refusals
    if mixed:
        assert [
            json_blob(recording, recording.events[event.parent].request_ref)["request"][
                "params"
            ]["expr"]
            for event in refusals
        ] == ["up", "down"]
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
            reply(
                tool_calls=first if mixed else [tool_call("fresh-one")],
                finish="tool_calls",
            ),
            reply(
                tool_calls=[repeated] if mixed else [tool_call("fresh-two")],
                finish="tool_calls",
            ),
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
        assert len(calls) == expected_calls and not unused_transport.called
        return outcome()

    capture = run_replay(recording, tmp_path, model=fresh_model, driver=replay_drive)
    assert capture.error is None, capture
    assert len(capture.id_map) == expected_calls + expected_refusals
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


@pytest.mark.parametrize("seed_cache", [False, True])
def test_removed_refusal_cannot_be_replaced_by_cached_denial(tmp_path, seed_cache):
    from opspilot.evaluation.codec import decode

    replies = [
        reply(tool_calls=[tool_call(name)], finish="tool_calls")
        for name in ("one", "two", "three")
    ]
    loop, req, _, _, _, _ = assemble(
        replies=[*replies, report_from_transcript], model_requests=4
    )
    ctx = replace(
        context(),
        source_run_id=req.run_id,
        started_at=loop.clock.now(),
        ended_at=loop.clock.now(),
        bindings=(
            replace(
                context().bindings[0],
                tool="metrics.range_query",
                target="checkout-prod",
                authorized_window=encode(req.scope.window),
            ),
        ),
    )

    def drive(hooks):
        loop.model = hooks.model(loop.model)
        loop.executor = hooks.tools(loop.executor)
        loop.capture_delivery = hooks.delivery
        assert loop.run(req).execution == "completed"
        return outcome()

    recording = source(tmp_path, driver=drive, ctx=ctx)
    meta = json.loads(recording.path.read_bytes())
    refusals = [item for item in meta["events"] if item["kind"] == "refusal"]
    deliveries = [item for item in meta["events"] if item["kind"] == "delivery"]

    def read(ref):
        return json.loads((recording.path.parent / ref["path"]).read_bytes())

    def put(ref, value):
        raw = canonical_bytes(value)
        ref.update(path="blobs/" + sha(raw), sha256=sha(raw), bytes=len(raw))
        (recording.path.parent / ref["path"]).write_bytes(raw)

    old_view = read(refusals[0]["response_ref"])
    last = deliveries[-1]
    message = read(last["response_ref"])
    message["content"] = canonical_bytes(old_view).decode()
    put(last["request_ref"], old_view)
    put(last["response_ref"], message)
    last.update(
        parent=None,
        hashes={
            "view_sha256": sha(canonical_bytes(old_view)),
            "delivery_sha256": sha(canonical_bytes(message)),
        },
    )
    final = [item for item in meta["events"] if item["kind"] == "model"][-1]
    model_request = read(final["request_ref"])
    for item in model_request["call"]["messages"]:
        if item.get("role") == "tool" and item.get("tool_call_id") == "three":
            item["content"] = message["content"]
    model_request["body"] = serialized_request(
        decode(ModelCall, model_request["call"])
    ).decode()
    put(final["request_ref"], model_request)
    final["hashes"] = {
        "request_body_sha256": sha(model_request["body"].encode()),
        "messages_sha256": sha(canonical_bytes(model_request["call"]["messages"])),
    }
    removed = refusals[-1]["seq"]
    meta["events"] = [item for item in meta["events"] if item["seq"] != removed]
    for seq, item in enumerate(meta["events"]):
        item["seq"] = seq
        if item["parent"] is not None and item["parent"] > removed:
            item["parent"] -= 1
    if seed_cache:
        meta["context"]["initial_state"]["evidence_views"] = [old_view]
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
    assert not model.calls


@pytest.mark.parametrize("kind", ["invalid_window", "too_large", "invalid_params"])
def test_real_unqueried_error_or_denial_records_and_replays_without_live_query(
    tmp_path, kind
):
    from tests.m1_tool_support import build, registration
    from tests.m1_tool_support import request as tool_request

    executor, transport, _, clock = build(
        registrations=[registration(max_window_seconds=300)]
    )
    req = tool_request()
    if kind == "invalid_window":
        req = replace(req, window={"start": "invalid", "end": "invalid"})
    elif kind == "invalid_params":
        req = replace(
            req,
            window={
                "start": req.window["start"],
                "end": (executor.scope.window.start + timedelta(minutes=1)).isoformat(),
            },
            params={"expr": 123},
        )
    counts = []

    class Counted:
        def execute(self, item):
            counts.append(item)
            result = executor.execute(item)
            assert not result.operation.dispatched and result.evidence is None
            assert result.status in ("error", "denied")
            return result

    ctx = replace(
        context(),
        source_run_id=executor.scope.run_id,
        started_at=clock.now(),
        ended_at=clock.now() + timedelta(seconds=10),
        bindings=(
            replace(
                context().bindings[0],
                tool=req.tool_name,
                target=req.target_ref,
                authorized_window=encode(executor.scope.window),
            ),
        ),
    )
    responses = [
        reply(
            tool_calls=[tool_call("refused", req.tool_name, json.dumps(req.params))],
            finish="tool_calls",
        ),
        reply(content="done"),
    ]

    ctx = _audit_context(ctx, executor)

    def exercise(model, tools, deliver, step):
        initial = call()
        response = model.complete(initial)
        result = tools.execute(replace(req, step_id=step))
        message = {
            "role": "tool",
            "tool_call_id": "refused",
            "content": canonical_bytes(result.model_view).decode(),
        }
        deliver(result.model_view, message)
        assistant = {
            "role": "assistant",
            "content": response.content,
            "reasoning_content": response.reasoning_content,
            "tool_calls": list(response.tool_calls),
        }
        model.complete(call([*initial.messages, assistant, message]))
        return replace(outcome(), evidence_ids=())

    if kind == "invalid_params":
        # The new ruling requires null window on unexecuted results. The
        # product keeps its parsed window on this late parameter refusal.
        with pytest.raises(RecordingError, match="IDENTITY_MISMATCH"):
            source(
                tmp_path,
                ctx=ctx,
                driver=lambda hooks: exercise(
                    hooks.model(ScriptedModel(responses)),
                    hooks.tools(Counted()),
                    hooks.delivery,
                    "source-step",
                ),
            )
        assert not transport.called
        return

    recording = source(
        tmp_path,
        ctx=ctx,
        driver=lambda hooks: exercise(
            hooks.model(ScriptedModel(responses)),
            hooks.tools(Counted()),
            hooks.delivery,
            "source-step",
        ),
    )
    assert recording.complete and not transport.called and len(counts) == 1
    manifest, attempt = manifest_attempt(recording, "structural")
    assert replay(
        recording,
        mode="structural",
        attempt=attempt,
        manifest=manifest,
        investigator=None,
    ).structural_verified
    capture = run_replay(
        recording,
        tmp_path,
        model=ScriptedModel(responses),
        driver=lambda session: exercise(
            session.model, session.tools, session.capture_delivery, "new-step"
        ),
    )
    assert capture.error is None and len(counts) == 1 and not transport.called
    copied = load_recording(
        tmp_path / "new-recording" / "recording.json",
        expected_hash=capture.recording_ref.sha256,
    )
    assert (
        json_blob(
            copied,
            next(item.response_ref for item in copied.events if item.kind == "tool"),
        )["evidence"]
        is None
    )


@pytest.mark.parametrize(
    "payload",
    [
        "evidence",
        "content",
        "rows",
        "evidence_id",
        "adopted",
        "citable_as_fact",
        "window",
        "tool",
    ],
)
def test_unqueried_result_cannot_smuggle_evidence_or_data(tmp_path, payload):
    def forge(req):
        base = Executor().execute(req)
        operation = replace(base.operation, dispatched=False, window=None)
        view = {
            "operation_id": operation.operation_id,
            "trust": "gateway",
            "status": "error",
            "reason": "INVALID_PARAMS",
            "source_contact": "none",
            "content": None,
            "citable_as_fact": False,
        }
        evidence = None
        if payload == "evidence":
            evidence = replace(
                base.evidence,
                operation=operation,
                _view=view,
                view_sha256=sha(canonical_bytes(view)),
            )
        elif payload == "content":
            view["content"] = [{"observed": "forged"}]
        elif payload == "rows":
            view["rows"] = [{"observed": "forged"}]
        elif payload == "evidence_id":
            view["evidence_id"] = "forged-evidence"
        elif payload in ("window", "tool"):
            view[payload] = {"rows": ["forged"]}
        else:
            view[payload] = True
        return ToolOutcome(operation, "error", "INVALID_PARAMS", "none", view, evidence)

    def drive(hooks):
        hooks.model(ScriptedModel([first_call()])).complete(call())
        tools = type("Forged", (), {"execute": staticmethod(forge)})()
        result = hooks.tools(tools).execute(request())
        hooks.delivery(
            result.model_view,
            {
                "role": "tool",
                "tool_call_id": "old-call",
                "content": canonical_bytes(result.model_view).decode(),
            },
        )
        return outcome()

    with pytest.raises(RecordingError, match="IDENTITY_MISMATCH"):
        source(tmp_path, driver=drive)


@pytest.mark.parametrize("default", [False, True])
def test_executed_without_evidence_still_requires_request_window(tmp_path, default):
    class BrokenWindow(Executor):
        def execute(self, req):
            base = super().execute(req)
            operation = replace(
                base.operation,
                window=Window(AUTH.start + timedelta(minutes=1), AUTH.end),
            )
            return ToolOutcome(
                operation,
                "error",
                "SOURCE_UNAVAILABLE",
                "possible",
                {
                    "operation_id": operation.operation_id,
                    "trust": "gateway",
                    "status": "error",
                    "reason": "SOURCE_UNAVAILABLE",
                    "source_contact": "possible",
                    "window": encode(operation.window),
                    "content": None,
                    "citable_as_fact": False,
                },
                None,
            )

    def drive(hooks):
        model = hooks.model(ScriptedModel([first_call(), reply(content="done")]))
        tools = hooks.tools(BrokenWindow())
        if default:
            original = tools

            class DefaultTools:
                def execute(self, req):
                    return original.execute(replace(req, window=None))

            tools = DefaultTools()
        return conversation(model, tools, hooks.delivery)

    with pytest.raises(RecordingError, match="IDENTITY_MISMATCH"):
        source(tmp_path, driver=drive)


@pytest.mark.parametrize("payload", ["rows", "content", "evidence_id", "unknown_data"])
def test_executed_without_evidence_cannot_deliver_data_even_with_matching_window(
    tmp_path, payload
):
    class MissingRecord(Executor):
        def execute(self, req):
            base = super().execute(req)
            view = {
                "operation_id": base.operation.operation_id,
                "window": encode(base.operation.window),
                "content": None,
                "citable_as_fact": False,
            }
            view[payload] = "forged-id" if payload == "evidence_id" else [{"value": 3}]
            return replace(base, evidence=None, model_view=view)

    with pytest.raises(RecordingError, match="IDENTITY_MISMATCH"):
        source(
            tmp_path,
            driver=lambda hooks: conversation(
                hooks.model(ScriptedModel([first_call(), reply(content="done")])),
                hooks.tools(MissingRecord()),
                hooks.delivery,
                stub=False,
            ),
        )


@pytest.mark.parametrize("mismatched", [False, True])
def test_saved_evidenceless_executed_packet_is_blocked_before_model(
    tmp_path, mismatched
):
    from opspilot.evaluation.recording import unwire, wire

    recording = source(tmp_path)
    meta = json.loads(recording.path.read_bytes())
    event = recording.events[1]
    old = unwire(ToolOutcome, json_blob(recording, event.response_ref))
    operation = (
        replace(
            old.operation, window=Window(AUTH.start + timedelta(minutes=1), AUTH.end)
        )
        if mismatched
        else old.operation
    )
    result = replace(
        old,
        operation=operation,
        evidence=None,
        model_view={**old.model_view, "window": encode(operation.window)},
    )
    payload = canonical_bytes(wire(result))
    ref = replace(
        event.response_ref,
        path="blobs/" + sha(payload),
        sha256=sha(payload),
        bytes=len(payload),
    )
    (recording.path.parent / ref.path).write_bytes(payload)
    meta["events"][1]["response_ref"] = encode(ref)
    meta["events"][1]["hashes"] = {
        "view_sha256": sha(canonical_bytes(result.model_view)),
        "raw_sha256": sha(b""),
    }
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
    assert not model.calls


@pytest.mark.parametrize("status", ["error", "no_data", "oversize"])
def test_executed_metadata_only_result_without_evidence_keeps_empty_raw_hash(
    tmp_path, status
):
    class EmptyResult(Executor):
        def execute(self, req):
            base = super().execute(req)
            actual_status = "error" if status == "oversize" else status
            reason = (
                "RESULT_TOO_LARGE"
                if status == "oversize"
                else ("SOURCE_UNAVAILABLE" if status == "error" else "NO_DATA")
            )
            operation = _audited_operation(base.operation, req)
            view = {
                "operation_id": operation.operation_id,
                "status": actual_status,
                "reason": reason,
                "source_contact": "confirmed",
                "trust": "gateway",
                "window": encode(base.operation.window),
                "citable_as_fact": False,
                "content": None,
            }
            if status == "oversize":
                from opspilot.tools.executor import _view_refusal_detail

                view.update(
                    _view_refusal_detail(100, 50, req.params, encode(operation.window))
                )
            return ToolOutcome(
                operation, actual_status, reason, "confirmed", view, None
            )

    recording = source(
        tmp_path,
        ctx=_audit_context(),
        driver=lambda hooks: conversation(
            hooks.model(ScriptedModel([first_call(), reply(content="done")])),
            hooks.tools(EmptyResult()),
            hooks.delivery,
            stub=False,
        ),
    )
    event = next(item for item in recording.events if item.kind == "tool")
    assert event.hashes["raw_sha256"] == sha(b"")
    assert recording.complete


def _audit_context(ctx=None, executor=None):
    from opspilot.tools import QueryScope, TargetRegistry, ToolRegistry
    from tests.m1_tool_support import registration, target

    ctx = ctx or context()
    if executor is None:
        tools = (
            registration(
                name="metrics.query", max_result_bytes=1024, max_view_tokens=50
            ),
        )
        targets = (target(target_id="target"),)
        scope = QueryScope(
            "scope",
            "incident",
            "target",
            "source-run",
            0,
            0,
            0,
            TargetRegistry(targets).revision,
            ToolRegistry(tools).revision,
            frozenset({"target"}),
            frozenset({"metrics.query"}),
            AUTH,
            NOW + timedelta(seconds=360),
        )
    else:
        tools = tuple(executor._tools._entries.values())
        targets = tuple(executor._targets._entries.values())
        scope = executor.scope
    manifest = Manifest(
        "source-round",
        {
            name: DimensionValue("hash", ctx.versions.get(name, HASH), None)
            for name in REQUIRED_DIMENSIONS
        },
    )
    return bind_tool_snapshots(ctx, (ToolSnapshot(tools, targets, scope),), manifest)


def _audited_operation(operation, req):
    ctx = _audit_context()
    snapshot = ctx.tool_snapshots[0]
    scope = snapshot.scope
    return replace(
        operation,
        scope_id=scope.scope_id,
        subject_kind=scope.subject_kind,
        subject_id=scope.subject_id,
        registry_revision=scope.registry_revision,
        tool_registry_revision=scope.tool_registry_revision,
        query=canonical_bytes(req.params).decode(),
        started_at=NOW,
        authorized_at=NOW,
        finished_at=NOW,
        elapsed_seconds=0.0,
        timeout_seconds=10.0,
        credential_ref=snapshot.targets[0].credential_ref,
    )


def _size_result(req, family, change=None):
    from types import SimpleNamespace

    from opspilot.tools.executor import _refusal_detail, _view_refusal_detail

    base = Executor().execute(req)
    operation = _audited_operation(base.operation, req)
    view = {
        "operation_id": operation.operation_id,
        "status": "error",
        "reason": "RESULT_TOO_LARGE",
        "source_contact": "confirmed",
        "trust": "gateway",
        "content": None,
        "adopted": False,
        "citable_as_fact": False,
        "window": encode(operation.window),
    }
    if family == "bytes":
        view.update(
            _refusal_detail(
                "RESULT_TOO_LARGE",
                SimpleNamespace(registration=SimpleNamespace(max_result_bytes=1024)),
            )
        )
    else:
        view.update(_view_refusal_detail(100, 50, req.params, encode(operation.window)))
    if change is not None:
        change(view)
    return ToolOutcome(operation, "error", "RESULT_TOO_LARGE", "confirmed", view, None)


def _size_source(tmp_path, family, change=None):
    class SizeExecutor:
        def execute(self, req):
            return _size_result(req, family, change)

    return source(
        tmp_path,
        ctx=_audit_context(),
        driver=lambda hooks: conversation(
            hooks.model(ScriptedModel([first_call(), reply(content="done")])),
            hooks.tools(SizeExecutor()),
            hooks.delivery,
            stub=False,
        ),
    )


@pytest.mark.parametrize("family", ["bytes", "view"])
@pytest.mark.parametrize(
    "mutation",
    [
        "source_data",
        "whitespace",
        "changed_limit",
        "missing_limit",
        "mixed_family",
        "message_only",
    ],
)
def test_result_limit_hints_must_match_complete_product_template(
    tmp_path, family, mutation
):
    def change(view):
        if mutation == "source_data":
            view["message"] = 'SOURCE DATA: {"rows":[{"value":3}]}'
        elif mutation == "whitespace":
            view["message"] += " "
        elif mutation == "changed_limit":
            view["max_result_bytes" if family == "bytes" else "max_view_tokens"] += 1
        elif mutation == "missing_limit":
            del view["max_result_bytes" if family == "bytes" else "max_view_tokens"]
        elif mutation == "mixed_family":
            view["max_view_tokens" if family == "bytes" else "max_result_bytes"] = 10
        else:
            for name in ("max_result_bytes", "max_view_tokens", "view_tokens", "query"):
                view.pop(name, None)

    with pytest.raises(RecordingError, match="IDENTITY_MISMATCH"):
        _size_source(tmp_path, family, change)


def test_product_limit_detail_output_bytes_are_fixed():
    from types import SimpleNamespace

    from opspilot.tools.executor import _refusal_detail, _view_refusal_detail

    expected_byte_message = "The result exceeded the 1024-byte limit for this tool. Retry with a smaller limit or a narrower window."
    expected_view_message = "The tool call result is too large to return: 100/50 tokens.\nTry to repeat the query but proactively narrow down the result (a narrower time window, a filter, or a smaller limit) so that the tool answer fits within the allowed number of tokens."
    byte_detail = _refusal_detail(
        "RESULT_TOO_LARGE",
        SimpleNamespace(registration=SimpleNamespace(max_result_bytes=1024)),
    )
    view_detail = _view_refusal_detail(100, 50, PARAMS, encode(AUTH))
    assert canonical_bytes(byte_detail) == canonical_bytes(
        {"max_result_bytes": 1024, "message": expected_byte_message}
    )
    assert canonical_bytes(view_detail) == canonical_bytes(
        {
            "window": encode(AUTH),
            "query": PARAMS,
            "max_view_tokens": 50,
            "view_tokens": 100,
            "message": expected_view_message,
        }
    )
    assert _refusal_detail("SOURCE_ERROR", SimpleNamespace()) is None


@pytest.mark.parametrize("family", ["bytes", "view"])
def test_valid_limit_hints_record_and_replay_without_evidence(tmp_path, family):
    recording = _size_source(tmp_path, family)
    event = next(item for item in recording.events if item.kind == "tool")
    assert recording.complete and event.hashes["raw_sha256"] == sha(b"")

    def drive(session):
        initial = call()
        response = session.model.complete(initial)
        result = session.tools.execute(request("new"))
        message = {
            "role": "tool",
            "tool_call_id": response.tool_calls[0]["id"],
            "content": canonical_bytes(result.model_view).decode(),
        }
        session.capture_delivery(result.model_view, message)
        assistant = {
            "role": "assistant",
            "content": response.content,
            "reasoning_content": response.reasoning_content,
            "tool_calls": list(response.tool_calls),
        }
        session.model.complete(call([*initial.messages, assistant, message]))
        return outcome()

    model = ScriptedModel(
        [first_call("new-call", reasoning="fresh"), reply(content="done")]
    )
    capture = run_replay(recording, tmp_path, model=model, driver=drive)
    assert capture.error is None and len(model.calls) == 2
    assert (
        json.loads(model.calls[-1].messages[-1]["content"])["message"]
        == json_blob(recording, event.response_ref)["model_view"]["message"]
    )


@pytest.mark.parametrize("family", ["bytes", "view"])
def test_self_consistent_forged_diagnostic_is_rejected_before_model(tmp_path, family):
    from opspilot.evaluation.codec import decode

    recording = _size_source(tmp_path, family)
    meta = json.loads(recording.path.read_bytes())
    tool = next(item for item in meta["events"] if item["kind"] == "tool")
    delivery = next(item for item in meta["events"] if item["kind"] == "delivery")
    model = [item for item in meta["events"] if item["kind"] == "model"][-1]

    def read(ref):
        return json.loads((recording.path.parent / ref["path"]).read_bytes())

    def put(ref, value):
        raw = canonical_bytes(value)
        ref.update(path="blobs/" + sha(raw), sha256=sha(raw), bytes=len(raw))
        (recording.path.parent / ref["path"]).write_bytes(raw)

    response = read(tool["response_ref"])
    response["model_view"]["message"] = 'SOURCE DATA: {"rows":[{"value":3}]}'
    view = response["model_view"]
    put(tool["response_ref"], response)
    tool["hashes"]["view_sha256"] = sha(canonical_bytes(view))
    put(delivery["request_ref"], view)
    message = read(delivery["response_ref"])
    message["content"] = canonical_bytes(view).decode()
    put(delivery["response_ref"], message)
    delivery["hashes"] = {
        "view_sha256": sha(canonical_bytes(view)),
        "delivery_sha256": sha(canonical_bytes(message)),
    }
    body = read(model["request_ref"])
    for item in body["call"]["messages"]:
        if item.get("role") == "tool":
            item["content"] = message["content"]
    body["body"] = serialized_request(decode(ModelCall, body["call"])).decode()
    put(model["request_ref"], body)
    model["hashes"] = {
        "request_body_sha256": sha(body["body"].encode()),
        "messages_sha256": sha(canonical_bytes(body["call"]["messages"])),
    }
    raw = canonical_bytes(meta)
    recording.path.write_bytes(raw)
    with pytest.raises(RecordingError, match="IDENTITY_MISMATCH"):
        load_recording(recording.path, expected_hash=sha(raw))
    candidate = replace(
        recording, ref=replace(recording.ref, sha256=sha(raw), bytes=len(raw))
    )
    client = ScriptedModel([])
    with pytest.raises(RecordingError, match="IDENTITY_MISMATCH"):
        run_replay(candidate, tmp_path, model=client)
    assert not client.calls


@pytest.mark.parametrize("mutation", ["joint", "operation", "view", "noncanonical"])
def test_evidenceless_query_is_bound_to_frozen_facade_params(tmp_path, mutation):
    payload = {"rows": [{"value": 3, "message": "absent from request"}]}

    class ForgedQuery:
        def execute(self, req):
            result = _size_result(req, "view")
            view = dict(result.model_view)
            operation = result.operation
            if mutation in ("joint", "operation"):
                operation = replace(operation, query=canonical_bytes(payload).decode())
            if mutation in ("joint", "view"):
                view["query"] = payload
            if mutation == "noncanonical":
                operation = replace(operation, query=" " + operation.query)
            return replace(result, operation=operation, model_view=view)

    with pytest.raises(RecordingError, match="IDENTITY_MISMATCH"):
        source(
            tmp_path,
            ctx=_audit_context(),
            driver=lambda hooks: conversation(
                hooks.model(ScriptedModel([first_call(), reply(content="done")])),
                hooks.tools(ForgedQuery()),
                hooks.delivery,
                stub=False,
            ),
        )


def _audit_source(tmp_path, factory, *, ctx=None, req=None, tick=None):
    ctx = ctx or _audit_context()
    req = req or request()

    class Facade:
        def execute(self, item):
            result = factory(item)
            if tick is not None:
                hooks.clock(tick)
            return result

    def driver(capture):
        nonlocal hooks
        hooks = capture
        model = hooks.model(
            ScriptedModel(
                [
                    reply(
                        tool_calls=[
                            tool_call(
                                "audit-call", req.tool_name, json.dumps(req.params)
                            )
                        ],
                        finish="tool_calls",
                    ),
                    reply(content="done"),
                ]
            )
        )
        initial = call()
        response = model.complete(initial)
        result = hooks.tools(Facade()).execute(req)
        message = {
            "role": "tool",
            "tool_call_id": "audit-call",
            "content": canonical_bytes(result.model_view).decode(),
        }
        hooks.delivery(result.model_view, message)
        assistant = {
            "role": "assistant",
            "content": response.content,
            "reasoning_content": response.reasoning_content,
            "tool_calls": list(response.tool_calls),
        }
        model.complete(call([*initial.messages, assistant, message]))
        return replace(outcome(), evidence_ids=())

    hooks = None
    return source(tmp_path, ctx=ctx, driver=driver)


@pytest.mark.parametrize(
    "field",
    [
        "operation_id",
        "run_id",
        "requested_tool",
        "requested_target",
        "scope_id",
        "subject_kind",
        "subject_id",
        "registry_revision",
        "tool_registry_revision",
        "tool",
        "tool_version",
        "source",
        "target_id",
        "credential_ref",
        "query",
        "window",
        "started_at",
        "authorized_at",
        "finished_at",
        "elapsed_seconds",
        "timeout_seconds",
        "dispatched",
    ],
)
def test_all_restricted_operation_fields_are_anchored_or_bounded(tmp_path, field):
    def forge(req):
        result = _size_result(req, "view")
        op, view = result.operation, dict(result.model_view)
        value = "unfrozen source payload"
        if field == "window":
            value = Window(AUTH.start + timedelta(seconds=1), AUTH.end)
        elif field in ("started_at", "authorized_at", "finished_at"):
            value = NOW + timedelta(seconds=11)
        elif field in ("elapsed_seconds", "timeout_seconds"):
            value = -1.0
        elif field == "dispatched":
            value = False
        op = replace(op, **{field: value})
        if field in view:
            view[field] = encode(value)
        if field == "query":
            op = replace(op, query=canonical_bytes({"rows": [3]}).decode())
            view["query"] = {"rows": [3]}
        if field == "started_at":
            view["requested_at"] = value.isoformat()
        return replace(result, operation=op, model_view=view)

    with pytest.raises(RecordingError, match="IDENTITY_MISMATCH"):
        _audit_source(tmp_path, forge)


@pytest.mark.parametrize(
    "case",
    [
        "byte_limit",
        "token_limit",
        "token_equal",
        "token_bool",
        "narrow_window",
        "status_pair",
        "contact_pair",
        "free_text",
        "extra_data",
        "time_order",
        "clock_lags",
        "timeout_limit",
        "timeout_deadline",
        "nonfinite_elapsed",
        "unknown_params",
        "invalid_param_type",
    ],
)
def test_joint_metadata_forgery_cannot_bypass_frozen_provenance(tmp_path, case):
    from opspilot.tools.executor import _result_size_message, _view_size_message

    req = request(params={"rows": [3]}) if case == "unknown_params" else request()
    if case == "invalid_param_type":
        req = request(params={"expr": 3})

    def forge(item):
        result = _size_result(item, "bytes" if case == "byte_limit" else "view")
        op, view = result.operation, dict(result.model_view)
        if case == "byte_limit":
            view.update(max_result_bytes=2048, message=_result_size_message(2048))
        elif case == "token_limit":
            view.update(max_view_tokens=51, message=_view_size_message(100, 51))
        elif case in ("token_equal", "token_bool"):
            tokens = 50 if case == "token_equal" else True
            view.update(view_tokens=tokens, message=_view_size_message(tokens, 50))
        elif case == "narrow_window":
            view["window"] = encode(Window(AUTH.start + timedelta(seconds=1), AUTH.end))
        elif case == "status_pair":
            view.update(status="denied", reason="TOOL_NOT_IN_SCOPE")
            result = replace(result, status="denied", reason="TOOL_NOT_IN_SCOPE")
        elif case == "contact_pair":
            view["source_contact"] = "possible"
            result = replace(result, source_contact="possible")
        elif case == "free_text":
            view["message"] = "SOURCE ROWS 3"
        elif case == "extra_data":
            view["observation"] = {"rows": [3]}
        elif case == "time_order":
            op = replace(op, started_at=NOW + timedelta(seconds=1))
        elif case == "clock_lags":
            op = replace(op, finished_at=NOW + timedelta(seconds=1))
        elif case == "timeout_limit":
            op = replace(op, timeout_seconds=11.0)
        elif case == "timeout_deadline":
            op = replace(op, authorized_at=NOW + timedelta(seconds=355))
        elif case == "nonfinite_elapsed":
            op = replace(op, elapsed_seconds=float("inf"))
        return replace(result, operation=op, model_view=view)

    # Nonfinite JSON cannot be captured in the first place; this is the codec
    # boundary, while every JSON-expressible mismatch is an identity failure.
    expected = "SCHEMA_INVALID" if case == "nonfinite_elapsed" else "IDENTITY_MISMATCH"
    with pytest.raises(RecordingError, match=expected):
        _audit_source(tmp_path, forge, req=req)


@pytest.mark.parametrize(
    "mutation", ["snapshot", "scope", "manifest", "binding", "missing"]
)
def test_snapshot_scope_manifest_and_binding_are_one_frozen_identity(
    tmp_path, mutation
):
    rec = _size_source(tmp_path, "view")
    meta = json.loads(rec.path.read_bytes())
    ctx = meta["context"]
    if mutation == "snapshot":
        ctx["tool_snapshots"][0]["tools"][0]["max_view_tokens"] = 51
    elif mutation == "scope":
        ctx["tool_snapshots"][0]["scope"]["subject_id"] = "unfrozen"
    elif mutation == "manifest":
        ctx["source_manifest"]["dimensions"]["registry"]["value"] = HASH
    elif mutation == "binding":
        ctx["bindings"][0]["permissions_hash"] = HASH
    else:
        ctx["tool_snapshots"] = []
        ctx["source_manifest"] = None
    raw = canonical_bytes(meta)
    rec.path.write_bytes(raw)
    with pytest.raises(RecordingError, match="IDENTITY_MISMATCH"):
        load_recording(rec.path, expected_hash=sha(raw))
    candidate = replace(rec, ref=replace(rec.ref, sha256=sha(raw), bytes=len(raw)))
    client = ScriptedModel([])
    with pytest.raises(RecordingError, match="IDENTITY_MISMATCH"):
        run_replay(candidate, tmp_path, model=client)
    assert not client.calls


def test_unknown_operation_field_is_identity_mismatch_even_with_recomputed_hash(
    tmp_path,
):
    rec = _size_source(tmp_path, "bytes")
    meta = json.loads(rec.path.read_bytes())
    event = next(e for e in meta["events"] if e["kind"] == "tool")
    body = json.loads((rec.path.parent / event["response_ref"]["path"]).read_bytes())
    body["operation"]["extra_source_data"] = {"rows": [3]}
    raw = canonical_bytes(body)
    path = "blobs/" + sha(raw)
    (rec.path.parent / path).write_bytes(raw)
    event["response_ref"].update(path=path, sha256=sha(raw), bytes=len(raw))
    raw = canonical_bytes(meta)
    rec.path.write_bytes(raw)
    with pytest.raises(RecordingError, match="IDENTITY_MISMATCH"):
        load_recording(rec.path, expected_hash=sha(raw))


@pytest.mark.parametrize(
    "case",
    [
        "empty",
        "source_error",
        "timeout",
        "unavailable",
        "transport_too_large",
        "body_too_large",
        "view_too_large",
        "malformed",
        "sink_failure",
    ],
)
def test_product_gateway_outcomes_with_independent_snapshots(tmp_path, case):
    from opspilot.tools import (
        TransportResponse,
        TransportResultTooLarge,
        TransportTimeout,
        TransportUnavailable,
    )
    from tests import m1_tool_support as t

    kwargs = {"token_counter": lambda _: 20000} if case == "view_too_large" else {}
    if case == "sink_failure":
        kwargs["sink"] = t.RecordingSink(fail=True)
    executor, transport, sink, clock = t.build(**kwargs)
    transport.response = TransportResponse(
        body=t.body([] if case == "empty" else [{"value": 3}])
    )
    if case == "source_error":
        transport.response = TransportResponse(body=t.body([]), source_status="500")
    elif case == "body_too_large":
        transport.response = TransportResponse(body=t.body([{"value": "x" * 5000}]))
    elif case == "malformed":
        transport.response = TransportResponse(body=b"xxx")
    errors = {
        "timeout": TransportTimeout,
        "unavailable": TransportUnavailable,
        "transport_too_large": TransportResultTooLarge,
    }
    if case in errors:
        transport.error = errors[case]()
    req = t.request()
    ctx = replace(
        context(),
        source_run_id=executor.scope.run_id,
        started_at=clock.now(),
        ended_at=clock.now() + timedelta(seconds=10),
        bindings=(
            replace(
                context().bindings[0],
                tool=req.tool_name,
                target=req.target_ref,
                authorized_window=encode(executor.scope.window),
            ),
        ),
    )
    ctx = _audit_context(ctx, executor)
    recording = _audit_source(tmp_path, executor.execute, ctx=ctx, req=req)
    assert recording.complete and transport.called
    event = next(e for e in recording.events if e.kind == "tool")
    body = json_blob(recording, event.response_ref)
    if case == "empty":
        assert body["evidence"] is not None
    else:
        assert body["evidence"] is None
        assert event.hashes["raw_sha256"] == sha(b"")


@pytest.mark.parametrize(
    "case",
    [
        "premature_deadline",
        "missing_elapsed",
        "invented_transport",
        "gateway_before_timeout",
        "dispatch_after_deadline",
    ],
)
def test_independent_audit_phase_regressions(tmp_path, case):
    def forge(req):
        result = _size_result(req, "view")
        op, view = result.operation, dict(result.model_view)
        if case == "missing_elapsed":
            return replace(result, operation=replace(op, elapsed_seconds=None))
        for key in ("message", "query", "max_view_tokens", "view_tokens"):
            view.pop(key, None)
        if case == "premature_deadline":
            result = replace(result, status="denied", reason="DEADLINE_EXCEEDED")
        elif case == "gateway_before_timeout":
            result = replace(result, status="timeout", reason="GATEWAY_TIMEOUT")
        elif case == "dispatch_after_deadline":
            op = replace(
                op,
                authorized_at=NOW + timedelta(seconds=360),
                finished_at=NOW + timedelta(seconds=360),
                timeout_seconds=0.0,
            )
        else:
            op = replace(
                op,
                dispatched=False,
                window=None,
                authorized_at=None,
                finished_at=None,
                elapsed_seconds=None,
                timeout_seconds=None,
                credential_ref=None,
            )
            result = replace(result, reason="SOURCE_UNAVAILABLE", source_contact="none")
        view.update(
            status=result.status,
            reason=result.reason,
            source_contact=result.source_contact,
            window=encode(op.window),
        )
        return replace(result, operation=op, model_view=view)

    with pytest.raises(RecordingError, match="IDENTITY_MISMATCH"):
        _audit_source(tmp_path, forge)


def test_legacy_complete_evidence_packet_without_new_context_fields_still_loads(
    tmp_path,
):
    rec = source(tmp_path)
    meta = json.loads(rec.path.read_bytes())
    del meta["context"]["tool_snapshots"]
    del meta["context"]["source_manifest"]
    raw = canonical_bytes(meta)
    rec.path.write_bytes(raw)
    assert load_recording(rec.path, expected_hash=sha(raw)).complete


@pytest.mark.parametrize("kind", ["ordinary", "gateway_timeout", "deadline"])
def test_bounded_audit_facts_allow_real_clock_progress(tmp_path, kind):
    ctx = replace(_audit_context(), ended_at=NOW + timedelta(seconds=400))
    finish = NOW + timedelta(
        seconds={"ordinary": 2, "gateway_timeout": 11, "deadline": 360}[kind]
    )

    def result(req):
        original = _size_result(req, "view")
        op = original.operation
        if kind == "ordinary":
            op = replace(
                op,
                authorized_at=NOW + timedelta(seconds=1),
                finished_at=finish,
                elapsed_seconds=2.0,
            )
            return replace(original, operation=op)
        for_state = (
            ("timeout", "GATEWAY_TIMEOUT")
            if kind == "gateway_timeout"
            else ("denied", "DEADLINE_EXCEEDED")
        )
        if kind == "deadline":
            op = replace(
                op,
                started_at=NOW + timedelta(seconds=359),
                authorized_at=NOW + timedelta(seconds=359),
                timeout_seconds=1.0,
                finished_at=finish,
                elapsed_seconds=1.0,
            )
        else:
            op = replace(op, finished_at=finish, elapsed_seconds=11.0)
        view = {
            key: value
            for key, value in original.model_view.items()
            if key not in {"message", "query", "max_view_tokens", "view_tokens"}
        }
        view.update(
            status=for_state[0],
            reason=for_state[1],
            requested_at=op.started_at.isoformat(),
        )
        return replace(
            original,
            operation=op,
            status=for_state[0],
            reason=for_state[1],
            model_view=view,
        )

    rec = _audit_source(tmp_path, result, ctx=ctx, tick=finish)
    assert rec.complete
    capture = run_replay(rec, tmp_path)
    assert capture.error is None
    copied = load_recording(
        tmp_path / "new-recording" / "recording.json",
        expected_hash=capture.recording_ref.sha256,
    )
    assert copied.context.started_at == ctx.started_at
    assert all(e.clock == ctx.ended_at for e in copied.events)


def test_snapshot_manifest_hash_dimension_cannot_be_relabelled_as_version(tmp_path):
    rec = _size_source(tmp_path, "view")
    manifest, attempt = manifest_attempt(rec)
    dims = dict(manifest.dimensions)
    dims["registry"] = replace(dims["registry"], kind="version")
    manifest = replace(manifest, dimensions=dims)
    attempt = replace(attempt, manifest_hash=manifest_hash(manifest))
    model = ScriptedModel([])
    capture = run_replay(rec, tmp_path, manifest=manifest, attempt=attempt, model=model)
    assert capture.error == "REPLAY_MISS" and not model.calls


@pytest.mark.parametrize("stage", ["dispatched", "no_authorization", "early_reserve"])
def test_deadline_reason_is_bound_to_the_actual_check_stage(tmp_path, stage):
    dispatched = stage == "dispatched"
    ctx = replace(_audit_context(), ended_at=NOW + timedelta(seconds=370))

    def forge(req):
        original = _size_result(req, "view")
        op = original.operation
        if not dispatched:
            op = replace(
                op,
                dispatched=False,
                window=None,
                authorized_at=None,
                finished_at=None,
                elapsed_seconds=None,
                timeout_seconds=None,
                credential_ref=None,
            )
            if stage == "early_reserve":
                op = replace(op, authorized_at=NOW)
        view = {
            key: value
            for key, value in original.model_view.items()
            if key not in {"message", "query", "max_view_tokens", "view_tokens"}
        }
        view.update(
            status="denied",
            reason="DEADLINE_EXCEEDED",
            source_contact="confirmed" if dispatched else "none",
            window=encode(op.window),
        )
        return replace(
            original,
            operation=op,
            model_view=view,
            status="denied",
            reason="DEADLINE_EXCEEDED",
            source_contact=view["source_contact"],
        )

    with pytest.raises(RecordingError, match="IDENTITY_MISMATCH"):
        _audit_source(tmp_path, forge, ctx=ctx, tick=NOW + timedelta(seconds=365))


@pytest.mark.parametrize("reserve_completed", [True, False])
def test_pre_dispatch_deadline_distinguishes_reserve_from_charge(
    tmp_path, reserve_completed
):
    ctx = replace(_audit_context(), ended_at=NOW + timedelta(seconds=370))

    def refuse(req):
        original = _size_result(req, "view")
        op = replace(
            original.operation,
            dispatched=False,
            window=None,
            finished_at=None,
            elapsed_seconds=None,
            authorized_at=NOW if reserve_completed else NOW + timedelta(seconds=365),
            timeout_seconds=10.0 if reserve_completed else None,
            credential_ref=original.operation.credential_ref
            if reserve_completed
            else None,
        )
        view = {
            key: value
            for key, value in original.model_view.items()
            if key not in {"message", "query", "max_view_tokens", "view_tokens"}
        }
        view.update(
            status="denied",
            reason="DEADLINE_EXCEEDED",
            source_contact="none",
            window=None,
        )
        return replace(
            original,
            operation=op,
            model_view=view,
            status="denied",
            reason="DEADLINE_EXCEEDED",
            source_contact="none",
        )

    assert _audit_source(
        tmp_path, refuse, ctx=ctx, tick=NOW + timedelta(seconds=365)
    ).complete


@pytest.mark.parametrize("mutation", ["possible_contact", "already_timed_out"])
def test_dispatched_deadline_has_arrival_phase_contact_and_timeout(tmp_path, mutation):
    ctx = replace(_audit_context(), ended_at=NOW + timedelta(seconds=370))

    def result(req):
        base = _size_result(req, "view")
        op = replace(
            base.operation,
            started_at=NOW + timedelta(seconds=359),
            authorized_at=NOW + timedelta(seconds=359),
            finished_at=NOW + timedelta(seconds=360),
            timeout_seconds=1.0,
            elapsed_seconds=2.0 if mutation == "already_timed_out" else 1.0,
        )
        view = {
            key: value
            for key, value in base.model_view.items()
            if key not in {"message", "query", "max_view_tokens", "view_tokens"}
        }
        contact = "possible" if mutation == "possible_contact" else "confirmed"
        view.update(status="denied", reason="DEADLINE_EXCEEDED", source_contact=contact)
        return replace(
            base,
            operation=op,
            model_view=view,
            status="denied",
            reason="DEADLINE_EXCEEDED",
            source_contact=contact,
        )

    with pytest.raises(RecordingError, match="IDENTITY_MISMATCH"):
        _audit_source(tmp_path, result, ctx=ctx, tick=NOW + timedelta(seconds=365))


@pytest.mark.parametrize(
    "prebound", [False, True], ids=["response_derived", "prebound_control"]
)
def test_snapshot_configuration_is_frozen_at_record_entry(tmp_path, prebound):
    from opspilot.tools import ToolRegistry
    from opspilot.tools.executor import _view_size_message

    base = _audit_context()
    response_limit = 777
    observations = []

    def snapshot(limit):
        previous = base.tool_snapshots[0]
        tools = (replace(previous.tools[0], max_view_tokens=limit),)
        scope = replace(
            previous.scope, tool_registry_revision=ToolRegistry(tools).revision
        )
        return ToolSnapshot(tools, previous.targets, scope)

    initial = (
        bind_tool_snapshots(
            context(), (snapshot(response_limit),), base.source_manifest
        )
        if prebound
        else context()
    )

    def driver(hooks):
        model = hooks.model(
            ScriptedModel(
                [
                    replace(
                        first_call(),
                        content=json.dumps({"max_view_tokens": response_limit}),
                    ),
                    reply(content="done"),
                ]
            )
        )
        first = call()
        response = model.complete(first)
        limit = json.loads(response.content)["max_view_tokens"]
        if not prebound:
            observations.append(
                (len(hooks.events), len(hooks.context.tool_snapshots), limit)
            )
            hooks.context = bind_tool_snapshots(
                hooks.context, (snapshot(limit),), base.source_manifest
            )
        frozen = hooks.context.tool_snapshots[0]

        class Facade:
            def execute(self, req):
                result = _size_result(req, "view")
                op = replace(
                    result.operation,
                    tool_registry_revision=frozen.scope.tool_registry_revision,
                )
                view = dict(
                    result.model_view,
                    max_view_tokens=limit,
                    view_tokens=limit + 1,
                    message=_view_size_message(limit + 1, limit),
                )
                return replace(result, operation=op, model_view=view)

        result = hooks.tools(Facade()).execute(request())
        message = {
            "role": "tool",
            "tool_call_id": response.tool_calls[0]["id"],
            "content": canonical_bytes(result.model_view).decode(),
        }
        hooks.delivery(result.model_view, message)
        assistant = {
            "role": "assistant",
            "content": response.content,
            "reasoning_content": response.reasoning_content,
            "tool_calls": list(response.tool_calls),
        }
        model.complete(call([*first.messages, assistant, message]))
        return replace(outcome(), evidence_ids=())

    if not prebound:
        with pytest.raises(RecordingError, match="IDENTITY_MISMATCH"):
            source(tmp_path, ctx=initial, driver=driver)
        assert observations == [(1, 0, response_limit)]
        doc = json.loads((tmp_path / "source" / "recording.json").read_bytes())
        assert doc["complete"] is False
        assert "IDENTITY_MISMATCH" in doc["incomplete_reasons"]
        assert (tmp_path / "source" / "INCOMPLETE").exists()
        with pytest.raises(RecordingError, match="RECORDING_INCOMPLETE"):
            load_recording(
                tmp_path / "source" / "recording.json",
                expected_hash=sha(canonical_bytes(doc)),
            )
    else:
        rec = source(tmp_path, ctx=initial, driver=driver)
        manifest, attempt = manifest_attempt(rec, "structural")
        assert (
            rec.complete
            and rec.context.tool_snapshots[0].tools[0].max_view_tokens == response_limit
        )
        assert replay(
            rec,
            mode="structural",
            manifest=manifest,
            attempt=attempt,
            investigator=None,
        ).structural_verified


@pytest.mark.parametrize(
    "mutation",
    ["versions_in_place", "binding", "manifest", "aborted", "unserializable"],
)
def test_record_entry_configuration_cannot_change_after_model_reply(tmp_path, mutation):
    def driver(hooks):
        hooks.model(ScriptedModel([reply(content="done")])).complete(call())
        if mutation == "binding":
            binding = replace(hooks.context.bindings[0], profile_hash=sha(b"changed"))
            hooks.context = replace(hooks.context, bindings=(binding,))
        elif mutation == "manifest":
            # The manifest remains self-consistent; its nested dimensions are
            # shared with the context used to construct CaptureHooks.
            hooks.context.source_manifest.dimensions["prompt"] = DimensionValue(
                "hash", sha(b"changed"), None
            )
            hooks.context = replace(
                hooks.context,
                source_manifest_hash=manifest_hash(hooks.context.source_manifest),
            )
        else:
            hooks.context.versions["code"] = (
                object() if mutation == "unserializable" else "after-response"
            )
            if mutation == "aborted":
                raise RuntimeError("synthetic driver failure")
        return replace(outcome(), evidence_ids=())

    with pytest.raises(RecordingError, match="IDENTITY_MISMATCH"):
        source(tmp_path, ctx=_audit_context(), driver=driver)
    assert (tmp_path / "source" / "INCOMPLETE").exists()
    path = tmp_path / "source" / "recording.json"
    if path.exists():
        assert json.loads(path.read_bytes())["complete"] is False


def test_record_entry_lock_allows_input_references_and_end_clock_to_advance(tmp_path):
    def driver(hooks):
        hooks.input(b'{"synthetic_original":true}')
        hooks.model(ScriptedModel([reply(content="done")])).complete(call())
        hooks.finish(NOW + timedelta(seconds=2))
        return replace(outcome(), evidence_ids=())

    rec = source(tmp_path, ctx=_audit_context(), driver=driver)
    assert rec.complete and rec.context.ended_at == NOW + timedelta(seconds=2)
    assert len(rec.context.input_refs) == 1
    assert json_blob(rec, rec.context.input_refs[0]) == {"synthetic_original": True}
