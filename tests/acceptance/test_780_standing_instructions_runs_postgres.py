"""#780 standing operator instructions: the tests that run the worker.

Same authorship, contract and harness as
``test_780_standing_instructions_postgres.py`` (its docstring lists the
sources and assumptions). Each test has a database of its own: the replayed
backends answer for the registered target ``TARGET_ID`` only, the rule of
one active entry per target would otherwise collide, and the lab server's
connection slots are few. Run with ``M1_DURABLE_POSTGRES=1``.
"""

from __future__ import annotations

import json
import os

import pytest

from opspilot.investigation.context import InvestigationInput, initial_messages
from opspilot.tools.otel_demo import TARGET_ID
from opspilot.tools.registry import canonical
from tests.acceptance.test_780_standing_instructions_postgres import (  # noqa: F401
    QUESTION,
    U,
    _body,
    _catalog_index,
    _harness,
    _marker,
    _revision_of,
    _run_once,
    _target,
    ca_file,
)
from tests.standing_instructions_support import (
    EXT_SWITCH_ON_PROMPT_REVISION,
    FACT,
    V7,
    V9,
    body_of,
    message_action,
    messages_containing,
    sent_text,
    si_publish,
    si_submitted,
    standing,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)


@pytest.fixture
def lone(ca_file):  # noqa: F811
    with _harness(ca_file, "opspilot_780_run") as harness:
        yield harness


# -- the model input (B4-9, B4-10, 780-3, 780-4) ----------------------------------


def test_the_instructions_are_one_user_message_before_the_runbook_catalog(lone):
    h, target = lone, TARGET_ID
    body = _body("msg")
    h.rb_publish(resource_uid=target, name=f"msg-rb-{U}", description="Checkout.")
    si_publish(h, body, resource_uid=target)
    incident, _ = h.submit(QUESTION, target_id=target)
    outcome, provider = _run_once(h, incident)

    first = provider.requests[0]["messages"]
    [index] = messages_containing(first, body)
    message = first[index]
    assert message["role"] == "user"
    # Its own message: not folded into the question, not the body alone.
    assert index >= 2
    assert QUESTION not in message["content"]
    assert message["content"] != body
    assert len(message["content"]) > len(body)
    catalog = _catalog_index(first)
    assert index < catalog
    # The evidence context still closes the fixed prefix.
    context = json.loads(first[catalog + 1]["content"])
    assert "time_policies" in context
    # Every later round repeats the prefix byte for byte (C3 §5) ...
    for request in provider.requests[1:]:
        assert canonical(request["messages"][: catalog + 2]) == canonical(
            first[: catalog + 2]
        )
    # ... and a rebuild from the recorded input reproduces it.
    parsed = InvestigationInput.from_json(h.recorded(incident))
    rebuilt, _system = initial_messages(parsed, evidence_context=context)
    assert canonical(rebuilt) == canonical(first[: catalog + 2])
    # The body is sent once per request, not again in the catalog.
    for request in provider.requests:
        assert len(messages_containing(request["messages"], body)) == 1
    # With instructions the boundary wording is part of the revision.
    assert _revision_of(outcome) != EXT_SWITCH_ON_PROMPT_REVISION


def test_without_approved_instructions_the_input_messages_and_revision_are_unchanged(
    lone,
):
    h, target = lone, TARGET_ID
    elsewhere = _target("none-other")
    h.rb_publish(resource_uid=target, name=f"none-rb-{U}", description="Checkout.")
    si_publish(h, _body("none-other"), resource_uid=elsewhere)
    rejected = si_submitted(h, _body("none-rej"), resource_uid=target)
    assert h.rb_review(rejected, "reject").status == 200
    revoked = si_publish(h, _body("none-rev"), resource_uid=target)
    h.rb_revoke(revoked["entry_id"], 1)
    si_submitted(h, _body("none-pending"), resource_uid=target)

    incident, _ = h.submit(QUESTION, target_id=target)
    recorded = h.recorded(incident)
    assert recorded["version"] == V7
    assert FACT not in recorded["scope_facts"]
    outcome, provider = _run_once(h, incident)

    first = provider.requests[0]["messages"]
    # The pre-#780 prefix: system, question, runbook catalog, evidence context.
    assert [m["role"] for m in first[:4]] == ["system", "user", "user", "user"]
    assert _catalog_index(first) == 2
    context = json.loads(first[3]["content"])
    rebuilt, _ = initial_messages(
        InvestigationInput.from_json(recorded), evidence_context=context
    )
    assert canonical(rebuilt) == canonical(first[:4])
    text = sent_text(provider)
    for label in ("none-other", "none-rej", "none-rev", "none-pending"):
        assert _marker(label) not in text, label
    assert _revision_of(outcome) == EXT_SWITCH_ON_PROMPT_REVISION


def test_the_boundary_counts_in_the_revision_and_the_body_does_not(lone):
    h = lone
    first = si_publish(h, _body("rev-a"))
    incident, _ = h.submit(QUESTION)
    outcome, _provider = _run_once(h, incident)
    revisions = [_revision_of(outcome)]
    sid = si_submitted(
        h, _body("rev-b") + "* One more check.\n", supersedes_entry_id=first["entry_id"]
    )
    assert h.rb_review(sid).status == 200
    incident, _ = h.submit(QUESTION)
    assert body_of(standing(h, incident)) == _body("rev-b") + "* One more check.\n"
    outcome, _provider = _run_once(h, incident)
    revisions.append(_revision_of(outcome))
    # Instance text, not template: a different body keeps the revision.
    assert revisions[0] == revisions[1]
    assert revisions[0] != EXT_SWITCH_ON_PROMPT_REVISION


def test_a_revoke_after_creation_leaves_the_run_and_successors_reread(lone):
    h, target = lone, TARGET_ID
    first = si_publish(h, _body("succ-1"), resource_uid=target)
    incident, run = h.submit(QUESTION, target_id=target)
    h.rb_revoke(first["entry_id"], 1)

    # The Run created before the revoke still sends what it recorded.
    _outcome, provider = _run_once(h, incident)
    first_messages = provider.requests[0]["messages"]
    assert len(messages_containing(first_messages, _body("succ-1"))) == 1
    assert body_of(standing(h, incident)) == _body("succ-1")

    # Follow-up: the continuation Run re-reads at creation (new instructions).
    second = si_publish(h, _body("succ-2"), resource_uid=target)
    asked = message_action(h, incident, "Also check the payment service.")
    assert 200 <= asked.status < 300, asked.text
    h.workbench.relay(incident)
    relayed = h.store.rebuild(incident)["run"]
    assert str(relayed["run_id"]) != str(run)
    fact = relayed["input"]["scope_facts"][FACT]
    assert str(fact["entry_id"]) == str(second["entry_id"])
    assert body_of(fact) == _body("succ-2")
    assert relayed["input"]["version"] == V9
    InvestigationInput.from_json(relayed["input"])

    # Re-investigate (cancel + new_run) on another incident: the successor
    # re-reads too, so a revoke in between removes the instructions.
    other, other_run = h.submit(QUESTION, target_id=target)
    assert body_of(standing(h, other)) == _body("succ-2")
    h.rb_revoke(second["entry_id"], 1)
    assert h.control(other, "cancel").status == 200
    renewed = h.control(other, "new_run")
    assert renewed.status == 200, renewed.text
    successor = h.store.rebuild(other)["run"]
    assert str(successor["run_id"]) != str(other_run)
    assert FACT not in successor["input"]["scope_facts"]
    assert successor["input"]["version"] != V9
    InvestigationInput.from_json(successor["input"])
