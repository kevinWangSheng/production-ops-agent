"""Q1/A7 public recording identity contracts, including issue #234 controls."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from opspilot.evaluation.live import registry_hash, scope_policy_hash

from opspilot.evaluation import (
    DimensionValue,
    RecordingError,
    StorageError,
    check_recording_binding,
    load_recording,
    manifest_hash,
    record,
    replay,
)
from opspilot.investigation.loop import ModelCall
from opspilot.tools.executor import ToolRequest
from opspilot.tools.registry import canonical
from tests.acceptance import eval_b2_support as b2
from tests.acceptance import eval_c_support as s
from tests.acceptance.test_eval_c_live_driver import offline  # noqa: F401


def source(tmp_path, *, result=None):
    destination = tmp_path / "source"
    ref = record(
        b2.source_driver(result=result), destination=destination, context=b2.context()
    )
    return load_recording(destination / ref.path, expected_hash=ref.sha256)


def q1_context():
    manifest = replace(b2.manifest(), round_id=s.ROUND)
    attempt = b2.attempt("live", manifest, product_run_id="CANARY-RUN")
    ctx = s.recording_context(attempt, manifest)
    snapshot = ctx.tool_snapshots[0]
    manifest = replace(
        manifest,
        dimensions={
            **manifest.dimensions,
            "registry": DimensionValue(
                "hash", registry_hash(snapshot.tools, snapshot.targets), None
            ),
            "permissions": DimensionValue(
                "hash", scope_policy_hash(snapshot.scope), None
            ),
        },
    )
    attempt = replace(attempt, manifest_hash=manifest_hash(manifest))
    from opspilot.evaluation import bind_tool_snapshots

    ctx = bind_tool_snapshots(ctx, ctx.tool_snapshots, manifest)
    return manifest, attempt, ctx


def test_q1_valid_per_attempt_binding_accepts_different_registry_and_permissions(
    tmp_path,
):
    manifest, attempt, ctx = q1_context()
    for dimension in ("registry", "permissions"):
        assert (
            ctx.source_manifest.dimensions[dimension] != manifest.dimensions[dimension]
        )
    ref = record(
        b2.source_driver(requests=[], result=b2.outcome(())),
        destination=tmp_path / "bound",
        context=ctx,
    )
    recording = load_recording(tmp_path / "bound" / ref.path, expected_hash=ref.sha256)
    check_recording_binding(
        recording, attempt, round_id=s.ROUND, epoch=7, manifest=manifest
    )


@pytest.mark.parametrize(
    "mismatch",
    [
        "recording-id",
        "source-run",
        "missing-product-run",
        "scenario-id",
        "scenario-version",
        "missing-snapshots",
        "snapshot-run",
        "snapshot-scope",
        "epoch",
    ],
)
def test_q1_each_identity_mismatch_is_rejected(tmp_path, mismatch):
    manifest, attempt, ctx = q1_context()
    ref = record(
        b2.source_driver(requests=[], result=b2.outcome(())),
        destination=tmp_path / "bound",
        context=ctx,
    )
    recording = load_recording(tmp_path / "bound" / ref.path, expected_hash=ref.sha256)
    epoch = 7
    if mismatch == "recording-id":
        ctx = replace(ctx, recording_id="CANARY-OTHER-RECORDING")
    elif mismatch == "source-run":
        ctx = replace(ctx, source_run_id="CANARY-OTHER-RUN")
    elif mismatch == "missing-product-run":
        attempt = replace(attempt, product_run_id=None)
    elif mismatch == "scenario-id":
        ctx = replace(ctx, scenario_id="CANARY-OTHER-SCENARIO")
    elif mismatch == "scenario-version":
        ctx = replace(ctx, scenario_version="CANARY-OTHER-VERSION")
    elif mismatch == "missing-snapshots":
        ctx = replace(ctx, tool_snapshots=())
    elif mismatch in {"snapshot-run", "snapshot-scope"}:
        snapshot = ctx.tool_snapshots[0]
        scope = replace(
            snapshot.scope,
            **{"run_id" if mismatch == "snapshot-run" else "scope_id": "CANARY-OTHER"},
        )
        ctx = replace(ctx, tool_snapshots=(replace(snapshot, scope=scope),))
    else:
        epoch = 8
    recording = replace(recording, context=ctx)
    with pytest.raises(StorageError, match="^IDENTITY_MISMATCH$"):
        check_recording_binding(
            recording, attempt, round_id=s.ROUND, epoch=epoch, manifest=manifest
        )


@pytest.mark.parametrize(
    "dimension",
    [name for name in b2.DIMENSIONS if name not in {"registry", "permissions"}],
)
def test_q1_every_other_manifest_dimension_must_match(tmp_path, dimension):
    manifest, attempt, ctx = q1_context()
    ref = record(
        b2.source_driver(requests=[], result=b2.outcome(())),
        destination=tmp_path / "bound",
        context=ctx,
    )
    recording = load_recording(tmp_path / "bound" / ref.path, expected_hash=ref.sha256)
    source_manifest = recording.context.source_manifest
    wrong = replace(
        source_manifest,
        dimensions={
            **source_manifest.dimensions,
            dimension: DimensionValue("version", "CANARY-DIFFERENT", None),
        },
    )
    recording = replace(
        recording,
        context=replace(
            recording.context,
            source_manifest=wrong,
            source_manifest_hash=manifest_hash(wrong),
        ),
    )
    with pytest.raises(StorageError, match="^IDENTITY_MISMATCH$"):
        check_recording_binding(
            recording, attempt, round_id=s.ROUND, epoch=7, manifest=manifest
        )


COMMON_VIEW = {
    "tool": b2.TOOL,
    "status": "ok",
    "content": [{"service": "cart", "error": True}],
}


def collision_driver(*, cite_both=False, count=2, with_identity=False, cache=False):
    """Legacy sparse views can be byte-identical despite distinct evidence IDs.

    Produce every result, then generate their deliveries in order. Only the
    first tool message enters the later physical model request (r2 erratum).
    """

    def driver(hooks):
        model = hooks.model(
            b2.FakeModel(
                [
                    b2.reply(
                        calls=tuple(f"call-{i}" for i in range(count)),
                        params=[b2.REQ_A["params"]] * count,
                    ),
                    b2.reply(calls=("cache-call",), params=[b2.REQ_A["params"]])
                    if cache
                    else b2.reply(content="done"),
                ]
                + ([b2.reply(content="done")] if cache else [])
            )
        )
        tools = hooks.tools(
            b2.FakeExecutor(
                [
                    lambda req, i=i: b2.tool_outcome(
                        req,
                        evidence_id=f"ev-{i + 1}",
                        view={
                            **COMMON_VIEW,
                            **({"evidence_id": f"ev-{i + 1}"} if with_identity else {}),
                        },
                    )
                    for i in range(count)
                ]
            )
        )
        hooks.clock(b2.START)
        call = ModelCall(
            messages=(
                {"role": "system", "content": b2.SYSTEM},
                {"role": "user", "content": b2.QUESTION},
            ),
            tools=(b2.TOOL_SCHEMA,),
            json_mode=False,
            max_tokens=100,
            timeout_seconds=30,
        )
        first = model.complete(call)
        results = []
        for i in range(count):
            hooks.clock(b2.START + (b2.END - b2.START) / 5 * (i + 1))
            results.append(
                tools.execute(ToolRequest(step_id="src", tool_index=i, **b2.REQ_A))
            )
        messages = []
        for i, result in enumerate(results):
            message = {
                "role": "tool",
                "tool_call_id": first.tool_calls[i]["id"],
                "content": canonical(dict(result.model_view)),
            }
            hooks.delivery(result.model_view, message)
            messages.append(message)
        second_call = replace(
            call,
            messages=call.messages
            + (
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": list(first.tool_calls),
                },
                messages[0],
            ),
        )
        second = model.complete(second_call)
        if cache:
            message = {
                "role": "tool",
                "tool_call_id": second.tool_calls[0]["id"],
                "content": canonical(dict(results[0].model_view)),
            }
            # Every physical tool retains its own parented delivery. This
            # additional delivery belongs to a new model call served from cache.
            hooks.delivery(results[0].model_view, message)
            model.complete(
                replace(
                    second_call,
                    messages=second_call.messages
                    + (
                        {
                            "role": "assistant",
                            "content": second.content,
                            "tool_calls": list(second.tool_calls),
                        },
                        message,
                    ),
                )
            )
        hooks.finish(b2.END)
        return b2.outcome(("ev-1", "ev-2") if cite_both else ("ev-1",))

    return driver


def test_234_identical_view_bytes_do_not_deliver_two_evidence_ids(tmp_path):
    with pytest.raises(RecordingError, match="^IDENTITY_MISMATCH$"):
        record(
            collision_driver(cite_both=True),
            destination=tmp_path / "collision",
            context=b2.context(),
        )


def test_234_concrete_parent_delivers_only_its_evidence_control(tmp_path):
    pkg = tmp_path / "collision"
    ref = record(collision_driver(), destination=pkg, context=b2.context())
    recording = load_recording(pkg / ref.path, expected_hash=ref.sha256)
    doc = b2.package_json(pkg)
    tools = b2.events_of(doc, "tool")
    deliveries = b2.events_of(doc, "delivery")
    assert len(tools) == len(deliveries) == 2
    assert max(event["seq"] for event in tools) < min(
        event["seq"] for event in deliveries
    )
    assert [event["parent"] for event in deliveries] == [
        event["seq"] for event in tools
    ]
    from opspilot.evaluation import evidence_from_recording

    evidence = evidence_from_recording(recording, outcome=b2.outcome(("ev-1",)))
    assert evidence.delivered["ev-1"].delivered is True
    assert "ev-2" not in evidence.delivered or not evidence.delivered["ev-2"].delivered


@pytest.mark.parametrize(
    ("count", "with_identity"), [(1, False), (2, False), (2, True)]
)
def test_234_cache_delivery_requires_unique_verifiable_identity(
    tmp_path, count, with_identity
):
    pkg = tmp_path / "cache"

    def create():
        return record(
            collision_driver(count=count, with_identity=with_identity, cache=True),
            destination=pkg,
            context=b2.context(),
        )

    if count == 2 and not with_identity:
        with pytest.raises(RecordingError, match="^IDENTITY_MISMATCH$"):
            create()
    else:
        ref = create()
        recording = load_recording(pkg / ref.path, expected_hash=ref.sha256)
        doc = b2.package_json(pkg)
        tools = b2.events_of(doc, "tool")
        deliveries = b2.events_of(doc, "delivery")
        assert len(tools) == count
        assert len(deliveries) == count + 1
        assert [delivery["parent"] for delivery in deliveries[:count]] == [
            tool["seq"] for tool in tools
        ]
        assert deliveries[-1]["parent"] is None
        from opspilot.evaluation import evidence_from_recording

        evidence = evidence_from_recording(recording, outcome=recording.outcome)
        assert evidence.delivered["ev-1"].delivered


def replay_driver(*, mutate=False, fail=False):
    def driver(session):
        b2.investigate(
            model=session.model,
            tools=session.tools,
            deliver=session.capture_delivery,
            requests=[b2.REQ_A],
            step_id="new-step",
            final_timeout=30,
        )
        if mutate:
            session.id_map["CANARY-FORGED-NEW"] = "CANARY-FORGED-OLD"
        if fail:
            raise RuntimeError("CANARY-FAILURE")
        return b2.outcome()

    return driver


@pytest.mark.parametrize("mutate", [False, True])
def test_234_replay_returns_id_map_from_verified_new_recording(tmp_path, mutate):
    src = source(tmp_path)
    model = b2.FakeModel(
        [
            b2.reply(calls=("new-call",), params=[b2.REQ_A["params"]]),
            b2.reply(content="done"),
        ]
    )
    capture = replay(
        src,
        mode="replay",
        attempt=b2.attempt(),
        manifest=b2.manifest(),
        investigator=model,
        driver=replay_driver(mutate=mutate),
        destination=tmp_path / "replayed",
    )
    assert capture.error is None
    assert dict(capture.id_map) == {"new-step-t0": "src-step-1-t0"}
    recording = load_recording(
        tmp_path / "replayed" / capture.recording_ref.path,
        expected_hash=capture.recording_ref.sha256,
    )
    assert dict(capture.id_map) == dict(
        recording.context.initial_state["replay_source"]["id_map"]
    )


def test_234_replay_failure_has_empty_id_map(tmp_path):
    src = source(tmp_path)
    model = b2.FakeModel(
        [
            b2.reply(calls=("new-call",), params=[b2.REQ_A["params"]]),
            b2.reply(content="done"),
        ]
    )
    capture = replay(
        src,
        mode="replay",
        attempt=b2.attempt(),
        manifest=b2.manifest(),
        investigator=model,
        driver=replay_driver(mutate=True, fail=True),
        destination=tmp_path / "failed",
    )
    assert capture.error is not None
    assert dict(capture.id_map) == {}


@pytest.mark.parametrize("send_original", [False, True])
def test_234_refusal_delivers_no_evidence_and_original_delivery_is_independent(
    tmp_path, send_original
):
    from opspilot.evaluation import evidence_from_recording
    from tests.acceptance.test_eval_b2_replay import _loop_context, _loop_driver

    physical_calls = []

    def driver(hooks):
        def model(client):
            captured = hooks.model(client)

            class RefusalOnlyModel:
                def complete(self, call):
                    # The original delivery is generated in both cases, but
                    # only the control sends its actual message to the model.
                    messages = tuple(
                        message
                        for message in call.messages
                        if send_original
                        or message.get("role") != "tool"
                        or json.loads(message["content"]).get("reason")
                        == "DUPLICATE_TOOL_CALL"
                    )
                    actual = replace(call, messages=messages)
                    physical_calls.append(actual)
                    return captured.complete(actual)

            return RefusalOnlyModel()

        result = _loop_driver({}, replay_session=False)(
            SimpleNamespace(model=model, tools=hooks.tools, delivery=hooks.delivery)
        )
        return result if send_original else b2.outcome(())

    pkg = tmp_path / "refusal"
    ref = record(driver, destination=pkg, context=_loop_context())
    recording = load_recording(pkg / ref.path, expected_hash=ref.sha256)
    doc = b2.package_json(pkg)
    deliveries = b2.events_of(doc, "delivery")
    by_seq = {event["seq"]: event for event in doc["events"]}
    original = next(
        event for event in deliveries if by_seq[event["parent"]]["kind"] == "tool"
    )
    refusal = next(
        event for event in deliveries if by_seq[event["parent"]]["kind"] == "refusal"
    )
    original_message = b2.blob_json(pkg, original["response_ref"])
    refusal_message = b2.blob_json(pkg, refusal["response_ref"])
    assert any(refusal_message in call.messages for call in physical_calls)
    assert (
        any(original_message in call.messages for call in physical_calls)
        is send_original
    )
    evidence = evidence_from_recording(recording, outcome=recording.outcome)
    delivered = [item for item in evidence.delivered.values() if item.delivered]
    # A refusal contributes zero evidence; only independently delivered
    # original tool messages can make their evidence count as delivered.
    assert len(delivered) == (1 if send_original else 0)


@pytest.mark.parametrize("mismatch", ["static-tools", "scope-policy"])
def test_q1_round_manifest_must_match_snapshot_static_registration_and_policy(
    tmp_path, mismatch
):
    manifest, attempt, ctx = q1_context()
    ref = record(
        b2.source_driver(requests=[], result=b2.outcome(())),
        destination=tmp_path / "bound",
        context=ctx,
    )
    recording = load_recording(tmp_path / "bound" / ref.path, expected_hash=ref.sha256)
    snapshot = recording.context.tool_snapshots[0]
    if mismatch == "static-tools":
        tools = (
            replace(snapshot.tools[0], version="CANARY-OTHER-VERSION"),
        ) + snapshot.tools[1:]
        dimension = "registry"
        wrong_hash = registry_hash(tools, snapshot.targets)
    else:
        from datetime import timedelta

        policy = replace(
            snapshot.scope,
            window=replace(
                snapshot.scope.window,
                end=snapshot.scope.window.end + timedelta(seconds=1),
            ),
        )
        dimension = "permissions"
        wrong_hash = scope_policy_hash(policy)
    assert wrong_hash != manifest.dimensions[dimension].value
    wrong = replace(
        manifest,
        dimensions={
            **manifest.dimensions,
            dimension: DimensionValue("hash", wrong_hash, None),
        },
    )
    attempt = replace(attempt, manifest_hash=manifest_hash(wrong))
    with pytest.raises(StorageError, match="^IDENTITY_MISMATCH$"):
        check_recording_binding(
            recording, attempt, round_id=s.ROUND, epoch=7, manifest=wrong
        )
