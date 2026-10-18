"""Implementer unit tests for eval PR-C (live kind/OTel scenario driver).

Interface: docs/tasks/2026-10-18-eval-pr-c-interface.md. Offline only: the
lab, PostgreSQL and model are fakes; the independent acceptance tests live
under tests/acceptance/ and are written from the interface, not from here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import kind_lab
from tests.test_kind_lab_fault import _fake_kubectl

FLAGS = {
    "flags": {
        "paymentFailure": {
            "state": "ENABLED",
            "variants": {"100%": 1, "90%": 0.95, "off": 0},
            "defaultVariant": "off",
        },
        "productCatalogFailure": {
            "state": "ENABLED",
            "variants": {"on": True, "off": False},
            "defaultVariant": "off",
        },
        "cartFailure": {
            "state": "ENABLED",
            "variants": {"on": True, "off": False},
            "defaultVariant": "off",
        },
        "adFailure": {
            "state": "ENABLED",
            "variants": {"on": True, "off": False},
            "defaultVariant": "off",
        },
    }
}


def _raw(data: dict = FLAGS) -> bytes:
    return json.dumps(data, indent=2).encode()


# -- A2: kind_lab fault --flag ---------------------------------------------------


@pytest.mark.parametrize("flag", sorted(kind_lab.FAULT_FLAGS))
def test_mutate_moves_only_the_named_whitelisted_flag(flag: str) -> None:
    variant = kind_lab.FAULT_FLAGS[flag]
    out = json.loads(kind_lab.mutate_flags(_raw(), variant, "off", flag=flag))
    assert out["flags"][flag]["defaultVariant"] == variant
    for other, entry in FLAGS["flags"].items():
        if other != flag:
            assert out["flags"][other] == entry


def test_mutate_refuses_a_flag_outside_the_whitelist() -> None:
    with pytest.raises(SystemExit):
        kind_lab.mutate_flags(_raw(), "on", "off", flag="adFailure")


def test_mutate_refuses_a_whitelisted_flag_missing_from_the_file() -> None:
    data = json.loads(_raw())
    del data["flags"]["cartFailure"]
    with pytest.raises(SystemExit):
        kind_lab.mutate_flags(_raw(data), "on", "off", flag="cartFailure")


def test_mutate_refuses_a_variant_the_file_does_not_define() -> None:
    data = json.loads(_raw())
    data["flags"]["cartFailure"]["variants"] = {"off": False}
    with pytest.raises(SystemExit):
        kind_lab.mutate_flags(_raw(data), "on", "off", flag="cartFailure")


def test_cli_flag_injects_its_whitelisted_variant_and_restores(
    tmp_path: Path, monkeypatch
) -> None:
    state = tmp_path / "live.json"
    state.write_text(json.dumps(FLAGS, indent=2) + "\n")
    kubectl = _fake_kubectl(tmp_path / "kubectl", state, apply_patches=True)
    monkeypatch.setattr(kind_lab, "HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(kind_lab, "KUBECTL", str(kubectl))
    argv = ["fault", "inject", "--experiment-id", "e1", "--flag", "cartFailure"]
    assert kind_lab.main(argv) == 0
    live = json.loads(state.read_text())["flags"]
    assert live["cartFailure"]["defaultVariant"] == "on"
    assert live["paymentFailure"]["defaultVariant"] == "off"
    assert kind_lab.main(["fault", "restore", "--experiment-id", "e1"]) == 0
    assert json.loads(state.read_text()) == FLAGS
    log = [
        json.loads(line)
        for line in (tmp_path / "history/e1/fault-log.jsonl").read_text().splitlines()
    ]
    assert [row["flag"] for row in log] == ["cartFailure", "cartFailure"]


def test_cli_refuses_a_flag_outside_the_whitelist() -> None:
    with pytest.raises(SystemExit):
        kind_lab.main(["fault", "inject", "--flag", "adFailure"])


# -- A5: public registration DTO -------------------------------------------------


def test_registration_dto_is_what_the_executor_factory_registers() -> None:
    from opspilot.tools import otel_demo
    from opspilot.tools.registry import TargetRegistry, ToolRegistry

    dto = otel_demo.otel_demo_registrations()
    assert dto.tools == otel_demo._registrations()
    assert dto.targets == (otel_demo._target(otel_demo.OtelDemoConfig()),)
    assert (
        ToolRegistry(dto.tools).revision
        == ToolRegistry(otel_demo._registrations()).revision
    )
    assert TargetRegistry(list(dto.targets)).revision
    with pytest.raises(AttributeError):
        dto.tools = ()  # type: ignore[misc]


# -- A7 / #234 P1: delivered evidence comes from the verified parent -------------


def _b2():
    from tests.unit import test_eval_b2_impl as b2

    return b2


class _AnonymousViews:
    """Synthetic executor whose views omit identity fields, so two results
    can be byte-identical while carrying different evidence IDs."""

    def __init__(self, ids):
        self.ids = list(ids)

    def execute(self, req):
        from dataclasses import replace

        b2 = _b2()
        base = b2.Executor().execute(req)
        evidence_id = self.ids.pop(0)
        view = {"rows": [{"value": 3}], "window": base.model_view["window"]}
        from opspilot.evaluation.codec import canonical_bytes

        evidence = replace(
            base.evidence,
            evidence_id=evidence_id,
            _view=view,
            view_sha256=b2.sha(canonical_bytes(view)),
        )
        return replace(base, model_view=view, evidence=evidence)


def _two_calls(first, second):
    from tests.m1_investigation_support import reply, tool_call

    b2 = _b2()
    return reply(
        tool_calls=[
            tool_call(first, "metrics.query", json.dumps(b2.PARAMS)),
            tool_call(second, "metrics.query", json.dumps({"expr": "up"})),
        ],
        finish="tool_calls",
        reasoning="r",
    )


def _message(call_id, view):
    from opspilot.evaluation.codec import canonical_bytes

    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": canonical_bytes(view).decode(),
    }


def _same_view_driver(cited, *, cache_after=False, ids=("e1", "e2")):
    from dataclasses import replace

    from tests.m1_investigation_support import reply

    b2 = _b2()
    script = [_two_calls("a", "b") if len(ids) == 2 else b2.first_call("a")]
    if cache_after:
        script.append(b2.first_call("c", params={"expr": "again"}))
    script.append(reply(content="report"))

    def drive(hooks):
        model = hooks.model(b2.ScriptedModel(script))
        tools = hooks.tools(_AnonymousViews(ids))
        first = model.complete(b2.call())
        results, messages = [], []
        for index, item in enumerate(first.tool_calls):
            params = b2.PARAMS if index == 0 else {"expr": "up"}
            req = replace(b2.request("step", params), tool_index=index)
            results.append(tools.execute(req))
            messages.append(_message(item["id"], results[-1].model_view))
        # Both results exist before either is delivered (the loop's order).
        for result, message in zip(results, messages, strict=True):
            hooks.delivery(result.model_view, message)
        assistant = {
            "role": "assistant",
            "content": first.content,
            "reasoning_content": first.reasoning_content,
            "tool_calls": list(first.tool_calls),
        }
        # Only the first tool message ever reaches the model.
        history = [*b2.call().messages, assistant, messages[0]]
        if cache_after:
            second = model.complete(b2.call(history))
            cached = _message(second.tool_calls[0]["id"], results[0].model_view)
            hooks.delivery(results[0].model_view, cached)
            history += [
                {
                    "role": "assistant",
                    "content": second.content,
                    "reasoning_content": second.reasoning_content,
                    "tool_calls": list(second.tool_calls),
                },
                cached,
            ]
        model.complete(b2.call(history))
        return replace(b2.outcome(), evidence_ids=cited)

    return drive


def test_byte_identical_views_do_not_deliver_each_others_evidence(tmp_path):
    from opspilot.evaluation import RecordingError

    b2 = _b2()
    with pytest.raises(RecordingError, match="^IDENTITY_MISMATCH$"):
        b2.source(tmp_path, driver=_same_view_driver(("e1", "e2")))


def test_only_the_actually_delivered_parent_evidence_counts(tmp_path):
    b2 = _b2()
    recording = b2.source(tmp_path, driver=_same_view_driver(("e1",)))
    assert recording.complete
    assert recording.outcome.evidence_ids == ("e1",)


def test_an_ambiguous_parentless_cache_delivery_is_refused(tmp_path):
    from opspilot.evaluation import RecordingError

    b2 = _b2()
    with pytest.raises(RecordingError, match="^IDENTITY_MISMATCH$"):
        b2.source(tmp_path, driver=_same_view_driver(("e1",), cache_after=True))


def test_a_unique_parentless_cache_delivery_is_accepted(tmp_path):
    b2 = _b2()
    recording = b2.source(
        tmp_path, driver=_same_view_driver(("e1",), cache_after=True, ids=("e1",))
    )
    deliveries = [e for e in recording.events if e.kind == "delivery"]
    assert [d.parent is None for d in deliveries] == [False, True]


# -- A7 / #234 P2: the returned id_map comes from the re-verified package -------


def test_driver_edits_to_the_session_map_do_not_reach_the_result(tmp_path):
    b2 = _b2()
    recording = b2.source(tmp_path)

    def drive(session):
        result = b2.conversation(
            session.model, session.tools, session.capture_delivery, step="new-step"
        )
        session.id_map["new-step-t0"] = "forged-old-op"
        session.id_map["invented"] = "old-step-t0"
        return result

    capture = b2.run_replay(recording, tmp_path, driver=drive)
    assert capture.error is None, capture
    assert capture.id_map == {"new-step-t0": "old-step-t0"}


def test_a_failed_replay_returns_no_unverified_map(tmp_path):
    b2 = _b2()
    recording = b2.source(tmp_path)

    def drive(session):
        b2.conversation(
            session.model, session.tools, session.capture_delivery, step="new-step"
        )
        raise RuntimeError("driver failed after a tool")

    capture = b2.run_replay(recording, tmp_path, driver=drive)
    assert capture.error == "DRIVER_ERROR"
    assert capture.id_map == {}
