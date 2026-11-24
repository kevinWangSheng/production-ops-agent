"""Shared helpers for the #780 contract tests: reviewed per-target standing
operator instructions as a model input.

Written by an independent test author from the contract only (open-issues
round R10; pre-review A section #780, defaults 780-1..780-11; B4-U3/U4 and
B4-9..B4-12), without reading the implementation. Everything goes through
the public seams the runbook feature already has: the signed-in workbench
JSON API for submit / approve / reject / revoke (``tests/runbook_fetch_support
.FetchHarness``), the Run input snapshot the web records, and the messages a
scripted provider receives.

Names taken from the pre-review table (``# ASSUMPTION:`` where the table
does not spell the wire name out):

* form field ``kind`` with ``standing_instructions`` (780-1's column values;
  780-9 "runbook 提交页加类别单选"); omitted means ``runbook``;
* the fixed entry name ``standing-instructions`` (780-2), sent explicitly;
* refusal code ``STANDING_INSTRUCTIONS_EXISTS`` (780-2);
* Run input ``opspilot-investigation-input-v9`` with scope fact
  ``standing_instructions`` carrying ``entry_id``, ``revision``,
  ``approved_at``, ``approved_by`` and the redacted body (780-5).
  ASSUMPTION: the body's key is ``content`` (the submission field and the
  revisions column carry that name); ``body_of`` is the one place to adapt.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from tests.m1_web_support import ORIGIN, basic, post_form
from tests.runbook_fetch_support import FetchHarness

KIND = "standing_instructions"
NAME = "standing-instructions"
FACT = "standing_instructions"
V7 = "opspilot-investigation-input-v7"
V9 = "opspilot-investigation-input-v9"
EXISTS = "STANDING_INSTRUCTIONS_EXISTS"
MAX_BYTES = 4096
#: The extension profile's revision with ``OPSPILOT_KNOWLEDGE_TOOL=1`` and no
#: standing instructions, computed on origin/main ``d0a430bc`` (before any
#: #780 code) as ``prompt_revision_versions("replay-candidate-k8s",
#: knowledge_tool=True)``. B4-10 / 780-4: a Run without instructions keeps it.
EXT_SWITCH_ON_PROMPT_REVISION = "prompt-replay-candidate-k8s-566b5c603da3"


def si_submit(h: FetchHarness, content: str, **overrides: Any):
    fields: dict[str, Any] = {
        "kind": KIND,
        "name": NAME,
        "description": "Standing checks for every investigation of this target.",
        "content": content,
        "source_label": "contract test",
        "source_ref": "tests/standing_instructions_support.py",
    }
    fields.update(overrides)
    return h.rb_submit(**fields)


def si_submitted(h: FetchHarness, content: str, **overrides: Any) -> str:
    response = si_submit(h, content, **overrides)
    assert response.status == 200, response.text
    return response.json()["submission_id"]


def si_publish(h: FetchHarness, content: str, **overrides: Any) -> dict[str, Any]:
    """Submit and approve; returns the approve result plus ``submission_id``."""
    submission_id = si_submitted(h, content, **overrides)
    response = h.rb_review(submission_id)
    assert response.status == 200, response.text
    return {**response.json(), "submission_id": submission_id, "content": content}


def standing(h: FetchHarness, incident: UUID) -> dict[str, Any] | None:
    return h.recorded(incident)["scope_facts"].get(FACT)


def body_of(fact: dict[str, Any]) -> str:
    # ASSUMPTION (see module docstring): the redacted body's key.
    value = fact["content"]
    assert isinstance(value, str), fact
    return value


def code_of(response) -> str | None:
    try:
        return response.json().get("code")
    except (ValueError, AttributeError):
        return None


def message_action(h: FetchHarness, incident: UUID, text: str, action="follow_up"):
    """A workbench message (contract B1: not fenced on the generation)."""
    return post_form(
        h.app,
        f"/incidents/{incident}/control",
        {"action": action, "text": text, "idempotency_key": f"si-{uuid4().hex}"},
        headers={**basic(), "origin": ORIGIN, "accept": "application/json"},
    )


def messages_containing(messages: list[dict[str, Any]], needle: str) -> list[int]:
    return [
        i
        for i, m in enumerate(messages)
        if needle in (m["content"] if isinstance(m.get("content"), str) else "")
    ]


def sent_text(provider) -> str:
    return json.dumps(provider.requests, ensure_ascii=False)
