"""Synthetic fifth-round binding and shared-invariant regression cases."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from opspilot.evaluation import RecordingError, load_recording
from opspilot.evaluation.codec import canonical_bytes, encode
from opspilot.evaluation.recording import json_blob, tool_key, unwire
from opspilot.investigation.loop import tool_request_for
from opspilot.tools.outcomes import ToolOutcome
from tests.unit import test_eval_b2_evidence_identity_impl as identity
from tests.unit import test_eval_b2_impl as b2


@pytest.mark.parametrize("entry", ["record", "load", "structural"])
@pytest.mark.parametrize("kind", ["unknown", "undelivered", "tail_only"])
def test_outcome_evidence_requires_recorded_and_delivered_event(tmp_path, entry, kind):
    def drive(hooks):
        if kind == "unknown":
            b2.conversation(
                hooks.model(
                    b2.ScriptedModel([b2.first_call(), b2.reply(content="done")])
                ),
                hooks.tools(b2.Executor()),
                hooks.delivery,
            )
            return replace(b2.outcome(), evidence_ids=("unrecorded-evidence",))
        model = hooks.model(b2.ScriptedModel([b2.first_call()]))
        response = model.complete(b2.call())
        result = hooks.tools(b2.Executor()).execute(b2.request())
        if kind == "tail_only":
            hooks.delivery(
                result.model_view,
                {
                    "role": "tool",
                    "tool_call_id": response.tool_calls[0]["id"],
                    "content": canonical_bytes(result.model_view).decode(),
                },
            )
        return b2.outcome()

    if entry == "record":
        with pytest.raises(RecordingError, match="^IDENTITY_MISMATCH$"):
            b2.source(tmp_path, driver=drive)
        return
    rec = b2.source(tmp_path)
    meta = json.loads(rec.path.read_bytes())
    if kind == "unknown":
        meta["outcome"]["evidence_ids"] = ["unrecorded-evidence"]
    else:
        meta["events"] = meta["events"][: 2 if kind == "undelivered" else 3]
    raw = canonical_bytes(meta)
    rec.path.write_bytes(raw)
    candidate = replace(rec, ref=replace(rec.ref, sha256=b2.sha(raw), bytes=len(raw)))
    with pytest.raises(RecordingError, match="^IDENTITY_MISMATCH$"):
        if entry == "load":
            load_recording(rec.path, expected_hash=candidate.ref.sha256)
        else:
            manifest, attempt = b2.manifest_attempt(candidate, "structural")
            b2.replay(
                candidate,
                mode="structural",
                manifest=manifest,
                attempt=attempt,
                investigator=None,
            )


@pytest.mark.parametrize("entry", ["record", "load"])
@pytest.mark.parametrize("typed", [False, True])
def test_facade_request_matches_assistant_typed_params(tmp_path, entry, typed):
    asked = {"expr": True} if typed else b2.PARAMS
    executed = {"expr": 1} if typed else {"expr": "different-query"}
    if entry == "record":

        def drive(hooks):
            return b2.conversation(
                hooks.model(
                    b2.ScriptedModel(
                        [b2.first_call(params=asked), b2.reply(content="done")]
                    )
                ),
                hooks.tools(b2.Executor()),
                hooks.delivery,
                params=executed,
            )

        with pytest.raises(RecordingError, match="^IDENTITY_MISMATCH$"):
            b2.source(tmp_path, driver=drive)
        return
    rec = b2.source(
        tmp_path,
        driver=lambda hooks: b2.conversation(
            hooks.model(
                b2.ScriptedModel(
                    [b2.first_call(params=asked), b2.reply(content="done")]
                )
            ),
            hooks.tools(b2.Executor()),
            hooks.delivery,
            params=asked,
        ),
    )
    meta = json.loads(rec.path.read_bytes())
    event = meta["events"][1]
    obj = json_blob(rec, rec.events[1].request_ref)
    req = replace(b2.request(), params=executed)
    obj["request"] = encode(req)
    obj["key"] = tool_key(rec.context, req, rec.context.bindings[0])
    raw = canonical_bytes(obj)
    ref = event["request_ref"]
    ref.update(path="blobs/" + b2.sha(raw), sha256=b2.sha(raw), bytes=len(raw))
    (rec.path.parent / ref["path"]).write_bytes(raw)
    raw = canonical_bytes(meta)
    rec.path.write_bytes(raw)
    with pytest.raises(RecordingError, match="^IDENTITY_MISMATCH$"):
        load_recording(rec.path, expected_hash=b2.sha(raw))


AUDIT_CASES = (
    "contact_none",
    "contact_possible",
    "reversed_time",
    "outside_clock",
    "negative_elapsed",
    "excess_timeout",
    "zero_timeout",
    "deadline_reason",
    "view_status",
    "view_trust",
    "missing_view_field",
    "view_observed",
    "view_boolean",
    "future_data_as_of",
    "view_window",
)


@pytest.mark.parametrize("entry", ["record", "load"])
def test_facade_target_comes_from_frozen_run_binding(tmp_path, entry):
    binding = b2.context().bindings[0]
    other = replace(binding, target="other-target")
    ctx = replace(
        b2.context(), stimulus={"target_id": binding.target}, bindings=(binding, other)
    )

    def moved(result):
        op = replace(
            result.operation, requested_target=other.target, target_id=other.target
        )
        view = {**result.model_view, "target_id": other.target}
        evidence = replace(
            result.evidence,
            operation=op,
            _view=view,
            view_sha256=b2.sha(canonical_bytes(view)),
        )
        return replace(result, operation=op, model_view=view, evidence=evidence)

    if entry == "record":

        def drive(hooks):
            captured = hooks.tools(
                type(
                    "Facade",
                    (),
                    {"execute": lambda self, req: moved(b2.Executor().execute(req))},
                )()
            )

            class Route:
                def execute(self, req):
                    return captured.execute(replace(req, target_ref=other.target))

            return b2.conversation(
                hooks.model(
                    b2.ScriptedModel([b2.first_call(), b2.reply(content="done")])
                ),
                Route(),
                hooks.delivery,
            )

        with pytest.raises(RecordingError, match="^IDENTITY_MISMATCH$"):
            b2.source(tmp_path, ctx=ctx, driver=drive)
        return
    rec = b2.source(tmp_path, ctx=ctx)
    result = unwire(ToolOutcome, json_blob(rec, rec.events[1].response_ref))
    identity.resign(rec, moved(result))
    meta = json.loads(rec.path.read_bytes())
    event = meta["events"][1]
    obj = json_blob(rec, rec.events[1].request_ref)
    req = replace(b2.request(), target_ref=other.target)
    obj.update(
        request=encode(req), binding=encode(other), key=tool_key(ctx, req, other)
    )
    raw = canonical_bytes(obj)
    ref = event["request_ref"]
    ref.update(path="blobs/" + b2.sha(raw), sha256=b2.sha(raw), bytes=len(raw))
    (rec.path.parent / ref["path"]).write_bytes(raw)
    raw = canonical_bytes(meta)
    rec.path.write_bytes(raw)
    with pytest.raises(RecordingError, match="^IDENTITY_MISMATCH$"):
        load_recording(rec.path, expected_hash=b2.sha(raw))


def test_product_request_reconstruction_keeps_reserved_params_and_scope_window(
    tmp_path,
):
    params = {
        "expr": "synthetic",
        "window": {"start": "not-an-envelope"},
        "target_ref": "not-a-target",
    }
    assistant = b2.first_call(params=params).tool_calls[0]
    expected = tool_request_for(
        "old-step", 0, assistant, target_ref="target", window=b2.encode(b2.AUTH)
    )
    assert expected.params == params and expected.target_ref == "target"
    assert expected.window == b2.encode(b2.AUTH)
    rec = b2.source(
        tmp_path,
        driver=lambda hooks: b2.conversation(
            hooks.model(
                b2.ScriptedModel(
                    [b2.first_call(params=params), b2.reply(content="done")]
                )
            ),
            hooks.tools(b2.Executor()),
            hooks.delivery,
            params=params,
        ),
    )
    assert rec.complete


def forge_audit(result, case):
    op, view, evidence = result.operation, dict(result.model_view), result.evidence
    changes = {}
    if case.startswith("contact_"):
        changes["source_contact"] = case.removeprefix("contact_")
    elif case == "reversed_time":
        op = replace(op, authorized_at=op.started_at - timedelta(seconds=1))
    elif case == "outside_clock":
        op = replace(op, started_at=op.started_at - timedelta(seconds=1))
        view["dispatch_started_at"] = op.started_at.isoformat()
    elif case == "negative_elapsed":
        op = replace(op, elapsed_seconds=-1.0)
    elif case == "excess_timeout":
        op = replace(op, timeout_seconds=100.0)
    elif case == "zero_timeout":
        op = replace(op, timeout_seconds=0.0)
    elif case == "deadline_reason":
        changes.update(status="denied", reason="DEADLINE_EXCEEDED")
        view.update(status="denied", adopted=False, citable_as_fact=False)
        evidence = replace(evidence, status="denied", adopted=False)
    elif case == "view_status":
        view["status"] = "no_data"
    elif case == "view_trust":
        view["trust"] = "gateway"
    elif case == "missing_view_field":
        del view["observed_at"]
    elif case == "view_observed":
        view["observed_at"] = (evidence.observed_at + timedelta(seconds=1)).isoformat()
    elif case == "view_boolean":
        view["adopted"] = 1
    elif case == "future_data_as_of":
        at = evidence.observed_at + timedelta(seconds=1)
        evidence = replace(evidence, data_as_of=at)
        view.update(data_as_of=at.isoformat(), freshness_seconds=-1.0)
    elif case == "view_window":
        view["window"] = {"start": "bad", "end": "bad"}
    evidence = replace(
        evidence, operation=op, _view=view, view_sha256=b2.sha(canonical_bytes(view))
    )
    return replace(result, operation=op, evidence=evidence, model_view=view, **changes)


@pytest.mark.parametrize("entry", ["record", "load"])
@pytest.mark.parametrize("case", AUDIT_CASES)
def test_evidenced_results_share_gateway_audit_invariants(tmp_path, entry, case):
    if entry == "record":
        with pytest.raises(RecordingError, match="^IDENTITY_MISMATCH$"):
            identity.product_source(
                tmp_path, change=lambda result: forge_audit(result, case)
            )
    else:
        rec = identity.product_source(tmp_path)
        event = next(e for e in rec.events if e.kind == "tool")
        old = unwire(ToolOutcome, json_blob(rec, event.response_ref))
        digest = identity.resign(rec, forge_audit(old, case))
        with pytest.raises(RecordingError, match="^IDENTITY_MISMATCH$"):
            load_recording(rec.path, expected_hash=digest)


def test_legacy_evidence_id_need_not_be_duplicated_in_the_view(tmp_path):
    class Legacy(b2.Executor):
        def execute(self, req):
            result = super().execute(req)
            view = dict(result.model_view)
            del view["evidence_id"]
            evidence = replace(
                result.evidence, _view=view, view_sha256=b2.sha(canonical_bytes(view))
            )
            return replace(result, evidence=evidence, model_view=view)

    rec = b2.source(
        tmp_path,
        driver=lambda hooks: b2.conversation(
            hooks.model(b2.ScriptedModel([b2.first_call(), b2.reply(content="done")])),
            hooks.tools(Legacy()),
            hooks.delivery,
            stub=False,
        ),
    )
    assert rec.outcome.evidence_ids == ("evidence-old",)
    manifest, attempt = b2.manifest_attempt(rec, "structural")
    assert b2.replay(
        rec, mode="structural", manifest=manifest, attempt=attempt, investigator=None
    ).structural_verified
