"""#839: tool snapshots recorded before 2026-11-19 still decode, hashes unchanged.

The fixture is one snapshot in the pre-#730/#741 shape. The pinned hashes were
computed by the code at e44499b2^ (the last commit that wrote this shape), so
they are the values archived recordings carry.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from opspilot.evaluation.codec import StorageError, canonical_bytes, decode, encode
from opspilot.evaluation.live import registry_hash, scope_policy_hash
from opspilot.evaluation.tool_provenance import ToolSnapshot, snapshot_hashes
from opspilot.tools.executor import QueryScope
from opspilot.tools.registry import ToolRegistration, canonical_hash

FIXTURE = Path(__file__).parent / "fixtures/eval_legacy_839/tool_snapshot.json"


def _legacy() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())["snapshot"]


def test_legacy_snapshot_decodes_and_keeps_recorded_hashes() -> None:
    raw = _legacy()
    snapshot = decode(ToolSnapshot, raw)

    assert canonical_bytes(snapshot) == canonical_bytes(raw)
    assert all(tool.citable_as_fact is True for tool in snapshot.tools)
    assert snapshot_hashes((snapshot,)) == (
        "9adb399da24bec124d55420677eda551e30a31f826af36d778c3aad9b79119ab",
        "ab9de390c27e149a743a1eb132cb27e498c2c9f5078becfe5efc4c01bc8f8038",
    )
    assert canonical_hash(encode(snapshot.scope)) == (
        "045ab78cb8fae4e96ab099315e6152eae56304d0586d2cc8342c56be13df0370"
    )
    assert registry_hash(snapshot.tools, snapshot.targets) == (
        "67f95cdb987614f214cdb5b4c4d597aa1e8125713f906293439b0e96050cf654"
    )
    assert scope_policy_hash(snapshot.scope) == (
        "e5ac8decc4eb6b4ac1aff8811dd121026576b5d2b3c344496b79b7dc1f411da5"
    )


def _unknown_scope_key(s: dict[str, Any]) -> None:
    s["scope"]["max_budget"] = 1


def _one_retired_key(s: dict[str, Any]) -> None:
    del s["scope"]["max_tool_seconds"]


def _unknown_tool_key(s: dict[str, Any]) -> None:
    s["tools"][0]["legacy_flag"] = True


def _missing_tool_key(s: dict[str, Any]) -> None:
    del s["tools"][0]["read_only"]


@pytest.mark.parametrize(
    "mutate",
    [_unknown_scope_key, _one_retired_key, _unknown_tool_key, _missing_tool_key],
)
def test_only_the_two_known_differences_are_accepted(mutate: Any) -> None:
    raw = copy.deepcopy(_legacy())
    mutate(raw)
    with pytest.raises(StorageError) as caught:
        decode(ToolSnapshot, raw)
    assert caught.value.code == "SCHEMA_INVALID"


def test_current_dtos_stay_strict() -> None:
    raw = _legacy()
    for annotation, value in (
        (QueryScope, raw["scope"]),
        (ToolRegistration, raw["tools"][0]),
    ):
        with pytest.raises(StorageError) as caught:
            decode(annotation, value)
        assert caught.value.code == "SCHEMA_INVALID"
