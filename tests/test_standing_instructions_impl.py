"""Implementer unit tests for the standing-instructions helpers (#780).

Internal shaping only: the fact builder and validator, the v9 input
version, the message position and the per-Run ``prompt_revision`` key. The
acceptance, contract and safety tests are written independently.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from opspilot.investigation.context import (
    INPUT_VERSION_STANDING_INSTRUCTIONS,
    RUNBOOK_CATALOG,
    STANDING_INSTRUCTIONS,
    STANDING_INSTRUCTIONS_BOUNDARY,
    ContextError,
    InvestigationInput,
    initial_messages,
)
from opspilot.investigation.limits import M1_FROZEN_LIMITS
from opspilot.investigation.loop import prompt_revision_versions
from opspilot.knowledge.catalog import build_catalog
from opspilot.knowledge.standing import (
    build_standing_instructions,
    valid_standing_instructions,
)

ENTRY = UUID("00000000-0000-4000-8000-000000000780")
APPROVED = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
ROW = {
    "entry_id": ENTRY,
    "revision": 2,
    "approved_at": APPROVED,
    "approved_by": "operator",
    "content": "* Check the payment queue depth.\n* password=hunter2-secret-value",
}


def _input(**facts: object) -> InvestigationInput:
    return InvestigationInput(
        question="Why is checkout slow?",
        model_requests=4,
        limits=M1_FROZEN_LIMITS,
        tool_schemas=(),
        evidence_context=None,
        variant_id="replay-candidate",
        scope_facts=dict(facts),
    )


def test_the_fact_is_redacted_and_valid() -> None:
    fact = build_standing_instructions(ROW)
    assert valid_standing_instructions(fact)
    assert fact["entry_id"] == str(ENTRY)
    assert fact["revision"] == 2
    assert fact["approved_at"] == APPROVED.isoformat()
    assert "hunter2-secret-value" not in fact["content"]
    assert "payment queue depth" in fact["content"]


@pytest.mark.parametrize(
    "edit",
    [
        {"revision": 0},
        {"revision": True},
        {"content": ""},
        {"content": "   "},
        {"content": "x" * 8193},
        {"entry_id": "not-a-uuid"},
        {"approved_at": "2026-11-24T10:00:00"},
        {"approved_by": ""},
        {"policy_version": "other"},
        {"extra": 1},
    ],
)
def test_an_edited_fact_is_refused(edit: dict[str, object]) -> None:
    fact = {**build_standing_instructions(ROW), **edit}
    assert not valid_standing_instructions(fact)
    with pytest.raises(ContextError):
        _input(**{STANDING_INSTRUCTIONS: fact})


def test_v9_message_sits_before_the_runbook_list() -> None:
    catalog = build_catalog(
        [
            {
                "name": "db-checks",
                "entry_id": ENTRY,
                "revision": 1,
                "description": "database checks",
                "approved_at": APPROVED,
                "age_seconds": 60,
            }
        ],
        retrieved_at=datetime(2026, 11, 24, 10, 1, tzinfo=timezone.utc),
        max_age_seconds=86400,
    )
    fact = build_standing_instructions(ROW)
    run_input = _input(**{RUNBOOK_CATALOG: catalog, STANDING_INSTRUCTIONS: fact})
    assert run_input.version == INPUT_VERSION_STANDING_INSTRUCTIONS
    assert InvestigationInput.from_json(run_input.as_json()) == run_input
    messages, _ = initial_messages(run_input, evidence_context=None)
    contents = [m["content"] for m in messages]
    standing = next(
        i
        for i, c in enumerate(contents)
        if c.startswith(STANDING_INSTRUCTIONS_BOUNDARY)
    )
    listing = next(i for i, c in enumerate(contents) if "db-checks" in c)
    assert standing < listing
    assert contents[standing] == f"{STANDING_INSTRUCTIONS_BOUNDARY}\n{fact['content']}"


def test_without_the_fact_the_input_and_messages_are_unchanged() -> None:
    plain = _input()
    assert plain.version != INPUT_VERSION_STANDING_INSTRUCTIONS
    messages, _ = initial_messages(plain, evidence_context=None)
    assert not any(
        STANDING_INSTRUCTIONS_BOUNDARY in str(m["content"]) for m in messages
    )


@pytest.mark.parametrize("knowledge_tool", [False, True])
def test_the_boundary_counts_only_when_the_run_carries_the_fact(
    knowledge_tool: bool,
) -> None:
    without = prompt_revision_versions(knowledge_tool=knowledge_tool)
    default = prompt_revision_versions(
        knowledge_tool=knowledge_tool, standing_instructions=False
    )
    carried = prompt_revision_versions(
        knowledge_tool=knowledge_tool, standing_instructions=True
    )
    assert without == default
    assert carried != without
