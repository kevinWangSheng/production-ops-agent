"""Independent contract tests for #851 without PostgreSQL: the marker a Run
input records when reading the target's standing instructions failed.

Written from the pre-review table ``docs/tasks/2026-11-24-851-prereview.md``
(851-2, 851-6..851-8) by a test author who did not implement it. The marker
is the exact two-key fact below under the ``standing_instructions`` key; it
validates as input v9, is not sent to the model, and every other shape that
is neither the marker nor a well-formed fact is ``INPUT_INVALID``.
No network, no model.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from opspilot.investigation.context import (
    ContextError,
    InvestigationInput,
    initial_messages,
)
from opspilot.tools.registry import canonical
from tests import test_standing_instructions_780_contract as c780
from tests import test_tool_expansion_c2b_contract as c2b

FACT = "standing_instructions"
POLICY = "opspilot-standing-instructions-v1"
#: 851-2: the whole marker, nothing else.
MARKER = {"policy_version": POLICY, "status": "unavailable"}

# The environment fixture of the #780 contract tests (switch and backend
# variables cleared, a placeholder CA).
_environment = c780._environment


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _with(fact: object) -> InvestigationInput:
    return c2b._with_facts(c780._v7_input(), **{FACT: fact})


def test_the_marker_validates_as_v9_and_reads_back_unchanged():
    built = _with(dict(MARKER))
    assert built.version == c780.V9
    recorded = json.loads(json.dumps(built.as_json()))
    assert recorded["scope_facts"][FACT] == MARKER
    again = InvestigationInput.from_json(recorded)
    assert canonical(again.as_json()) == canonical(built.as_json())


@pytest.mark.parametrize(
    "fact",
    [
        {"policy_version": POLICY, "status": "ok"},
        {"policy_version": POLICY, "status": None},
        {"policy_version": POLICY},
        {"status": "unavailable"},
        {
            "policy_version": "opspilot-standing-instructions-v0",
            "status": "unavailable",
        },
        {**MARKER, "content": "* Always check pod restarts first.\n"},
        {**MARKER, "reason": "TIMEOUT"},
        {**MARKER, "entry_id": None, "revision": None},
        "unavailable",
        None,
    ],
    ids=[
        "status-ok",
        "status-null",
        "no-status",
        "no-policy",
        "wrong-policy",
        "marker-with-content",
        "marker-with-reason",
        "marker-with-null-fields",
        "bare-string",
        "null",
    ],
)
def test_a_malformed_marker_is_input_invalid(fact):
    with pytest.raises(ContextError) as raised:
        _with(fact)
    assert raised.value.code == "INPUT_INVALID"
    # A recorded row with it is refused the same way when read back.
    recorded = json.loads(json.dumps(c780._v7_input().as_json()))
    recorded["scope_facts"][FACT] = fact
    recorded["version"] = c780.V9
    with pytest.raises(ContextError) as again:
        InvestigationInput.from_json(recorded)
    assert again.value.code == "INPUT_INVALID"


def test_the_marker_sends_no_message():
    # 851-8: the model sees exactly what a Run without standing instructions
    # sees -- the v7 catalog input's messages on main.
    built = _with(dict(MARKER))
    messages, _ = initial_messages(built, evidence_context=c780.CONTEXT)
    assert _sha(canonical(messages)) == c780.V7_MESSAGES_SHA256
    rebuilt, _ = initial_messages(
        InvestigationInput.from_json(json.loads(json.dumps(built.as_json()))),
        evidence_context=c780.CONTEXT,
    )
    assert _sha(canonical(rebuilt)) == c780.V7_MESSAGES_SHA256
