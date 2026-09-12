import pytest

from scripts.m0.protocol import ProtocolError
from scripts.m0.step_store import _validated_tool_calls


@pytest.mark.parametrize(
    "assistant",
    [
        None,
        {"tool_calls": {}},
        {"tool_calls": ["bad"]},
        {"tool_calls": [{}]},
        {"tool_calls": [{"id": 123}]},
    ],
)
def test_malformed_tool_call_containers_fail_closed(assistant):
    with pytest.raises(ProtocolError, match="(ASSISTANT_INVALID|TOOL_CALLS_INVALID)"):
        _validated_tool_calls(assistant)


def test_valid_tool_call_container_is_returned_unchanged():
    calls = [{"id": "call-1", "type": "function"}]
    assert _validated_tool_calls({"tool_calls": calls}) is calls
