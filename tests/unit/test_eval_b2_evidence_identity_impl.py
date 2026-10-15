"""Frozen authorization identity for synthetic full EvidenceRecord packets."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from opspilot.evaluation import RecordingError, load_recording
from opspilot.evaluation.codec import canonical_bytes, decode
from opspilot.evaluation.recording import json_blob, unwire, wire
from opspilot.investigation.loop import ModelCall, serialized_request
from opspilot.tools import TransportResponse
from opspilot.tools.outcomes import ToolOutcome
from tests import m1_tool_support as t
from tests.unit import test_eval_b2_impl as b2

FIELDS = (
    "run_id",
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
)


def product_source(tmp_path, status="ok", change=None):
    executor, transport, _, clock = t.build()
    transport.response = TransportResponse(
        body=t.body([{"value": 3}] if status == "ok" else [])
    )
    req = t.request()
    ctx = replace(
        b2.context(),
        source_run_id=executor.scope.run_id,
        started_at=clock.now(),
        ended_at=clock.now() + timedelta(seconds=10),
        bindings=(
            replace(
                b2.context().bindings[0],
                tool=req.tool_name,
                target=req.target_ref,
                authorized_window=executor.scope.window.as_json(),
            ),
        ),
    )
    ctx = b2._audit_context(ctx, executor)

    def execute(request):
        result = executor.execute(request)
        assert result.evidence is not None and result.status == status
        return change(result) if change else result

    return b2._audit_source(tmp_path, execute, ctx=ctx, req=req)


def forge(result, field, *, view_only=False):
    operation = result.operation
    value = (
        b2.sha(b"wrong-revision") if field.endswith("revision") else "wrong-identity"
    )
    if field == "subject_kind":
        value = "release"
    if field == "query":
        value = canonical_bytes({"expr": "wrong-query"}).decode()
    if not view_only:
        operation = replace(operation, **{field: value})
    view = dict(result.model_view)
    if field in view or view_only:
        view[field] = json.loads(value) if field == "query" else value
    evidence = replace(
        result.evidence,
        operation=operation,
        _view=view,
        view_sha256=b2.sha(canonical_bytes(view)),
    )
    return replace(result, operation=operation, model_view=view, evidence=evidence)


def resign(recording, result):
    meta = json.loads(recording.path.read_bytes())
    tool = next(e for e in meta["events"] if e["kind"] == "tool")
    delivery = next(e for e in meta["events"] if e["kind"] == "delivery")
    model = [e for e in meta["events"] if e["kind"] == "model"][-1]

    def read(ref):
        return json.loads((recording.path.parent / ref["path"]).read_bytes())

    def put(ref, value):
        raw = canonical_bytes(value)
        ref.update(path="blobs/" + b2.sha(raw), sha256=b2.sha(raw), bytes=len(raw))
        (recording.path.parent / ref["path"]).write_bytes(raw)

    view = result.model_view
    put(tool["response_ref"], wire(result))
    tool["hashes"]["view_sha256"] = b2.sha(canonical_bytes(view))
    put(delivery["request_ref"], view)
    message = read(delivery["response_ref"])
    message["content"] = canonical_bytes(view).decode()
    put(delivery["response_ref"], message)
    delivery["hashes"] = {
        "view_sha256": b2.sha(canonical_bytes(view)),
        "delivery_sha256": b2.sha(canonical_bytes(message)),
    }
    body = read(model["request_ref"])
    for message in body["call"]["messages"]:
        if message["role"] == "tool":
            message["content"] = canonical_bytes(view).decode()
    body["body"] = serialized_request(decode(ModelCall, body["call"])).decode()
    put(model["request_ref"], body)
    model["hashes"] = {
        "request_body_sha256": b2.sha(body["body"].encode()),
        "messages_sha256": b2.sha(canonical_bytes(body["call"]["messages"])),
    }
    raw = canonical_bytes(meta)
    recording.path.write_bytes(raw)
    return b2.sha(raw)


@pytest.mark.parametrize("entry", ["record", "load"])
@pytest.mark.parametrize("status", ["ok", "no_data"])
@pytest.mark.parametrize("field", FIELDS)
def test_full_evidence_joint_identity_forgery_rejected(tmp_path, entry, status, field):
    def change(result):
        return forge(result, field)

    if entry == "record":
        with pytest.raises(RecordingError, match="^IDENTITY_MISMATCH$"):
            product_source(tmp_path, status, change)
        meta = json.loads((tmp_path / "source" / "recording.json").read_bytes())
        assert meta["complete"] is False
    else:
        rec = product_source(tmp_path, status)
        event = next(e for e in rec.events if e.kind == "tool")
        original = unwire(ToolOutcome, json_blob(rec, event.response_ref))
        digest = resign(rec, change(original))
        with pytest.raises(RecordingError, match="^IDENTITY_MISMATCH$"):
            load_recording(rec.path, expected_hash=digest)


@pytest.mark.parametrize("entry", ["record", "load"])
@pytest.mark.parametrize("field", FIELDS)
def test_full_evidence_view_identity_forgery_rejected(tmp_path, entry, field):
    def change(result):
        return forge(result, field, view_only=True)

    if entry == "record":
        with pytest.raises(RecordingError, match="^IDENTITY_MISMATCH$"):
            product_source(tmp_path, change=change)
    else:
        rec = product_source(tmp_path)
        event = next(e for e in rec.events if e.kind == "tool")
        original = unwire(ToolOutcome, json_blob(rec, event.response_ref))
        digest = resign(rec, change(original))
        with pytest.raises(RecordingError, match="^IDENTITY_MISMATCH$"):
            load_recording(rec.path, expected_hash=digest)


@pytest.mark.parametrize("status", ["ok", "no_data"])
def test_full_evidence_frozen_identity_positive_control(tmp_path, status):
    rec = product_source(tmp_path, status)
    manifest, attempt = b2.manifest_attempt(rec, "structural")
    assert b2.replay(
        rec, mode="structural", manifest=manifest, attempt=attempt, investigator=None
    ).structural_verified
