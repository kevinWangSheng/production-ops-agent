"""Tool-call / ToolOperation message pairing.

Technical plan sections 5 and 7: every call has a result, reconstruction
leaves no dangling pair, and unknown provider fields are not forwarded.
"""

import pytest

from opspilot.investigation.messages import (
    PairingError,
    assistant_message,
    pair_tool_results,
    validate_tool_calls,
)
from opspilot.investigation.reports import REPORT_CONTRACT
from tests.m1_investigation_support import (
    assemble,
    reply,
    report_from_transcript,
    tool_call,
)


def test_valid_pair_is_one_to_one_and_drops_unknown_fields():
    assistant = assistant_message(
        content="",
        reasoning_content="keep-me",
        tool_calls=[tool_call(), tool_call("call-2", arguments='{"expr":"other"}')],
    )
    assistant["extra_provider_field"] = "drop"
    results = [
        {"role": "tool", "tool_call_id": "call-2", "content": '{"ok":2}'},
        {"role": "tool", "tool_call_id": "call-1", "content": '{"ok":1}'},
    ]
    group = pair_tool_results(assistant, results, require_reasoning=True)
    assert [m["role"] for m in group] == ["assistant", "tool", "tool"]
    assert [m["tool_call_id"] for m in group[1:]] == ["call-1", "call-2"]
    assert "extra_provider_field" not in group[0]
    assert group[0]["reasoning_content"] == "keep-me"


def test_mismatched_ids_are_invalid_and_do_not_execute_tools():
    assistant = assistant_message(
        content="",
        reasoning_content="x",
        tool_calls=[tool_call()],
    )
    with pytest.raises(PairingError, match="TOOL_PAIRING_INVALID"):
        pair_tool_results(
            assistant,
            [{"role": "tool", "tool_call_id": "other", "content": "{}"}],
            require_reasoning=True,
        )


def test_duplicate_tool_call_ids_are_invalid():
    with pytest.raises(PairingError, match="TOOL_PAIRING_INVALID"):
        validate_tool_calls(
            {
                "role": "assistant",
                "tool_calls": [tool_call("dup"), tool_call("dup")],
            }
        )


def test_non_list_tool_calls_container_is_invalid():
    with pytest.raises(PairingError, match="TOOL_PAIRING_INVALID"):
        validate_tool_calls({"role": "assistant", "tool_calls": {}})


def test_missing_reasoning_content_is_private_protocol_failure():
    assistant = assistant_message(
        content="", reasoning_content=None, tool_calls=[tool_call()]
    )
    results = [{"role": "tool", "tool_call_id": "call-1", "content": "{}"}]
    with pytest.raises(PairingError, match="PRIVATE_PROTOCOL_MISSING"):
        pair_tool_results(assistant, results, require_reasoning=True)


def test_loop_does_not_execute_tools_when_pairing_is_invalid():
    loop, request, model, transport, store, _ = assemble(
        replies=[
            reply(tool_calls=[tool_call("a"), tool_call("a")], finish="tool_calls")
        ]
    )
    outcome = loop.run(request)
    assert outcome.execution == "failed"
    assert outcome.handoff is True
    assert outcome.handoff_reasons == ("TOOL_PAIRING_INVALID",)
    assert outcome.model_requests_used == 1
    assert outcome.steps_committed == 1
    assert transport.called is False
    assert store.tool_results == {} or all(
        not items for items in store.tool_results.values()
    )


def test_loop_pairs_executor_views_into_the_next_model_request():
    loop, request, model, transport, _, _ = assemble(
        replies=[
            reply(tool_calls=[tool_call()], finish="tool_calls"),
            report_from_transcript,
        ]
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed"
    assert outcome.handoff is False
    assert transport.called is True
    assert len(model.calls) == 2
    roles = [m["role"] for m in model.calls[1].messages]
    assert roles[-3:] == ["assistant", "tool", "user"]
    tool_msg = model.calls[1].messages[-2]
    assert tool_msg["tool_call_id"] == "call-1"
    assert "evidence_id" in tool_msg["content"]
    assert model.calls[1].messages[-3]["reasoning_content"] == "reasoned"


def test_report_contract_contains_the_vendor_required_json_example():
    assert "json" in REPORT_CONTRACT
    assert "m0-report-v2" in REPORT_CONTRACT
    assert "schema_version" in REPORT_CONTRACT
