"""Credentials in a model-written task list never reach model inputs or the
stored record (PRODUCT-CONSTRAINTS "Data flow contract"; C1 #565 §1: task
content passes ``redact_credentials`` before it is stored and returned).

``TodoWrite`` is a new model input channel: the model's own task text comes
back to it in the next request and is stored with the step. Driven through
the real investigation loop with a scripted model and the in-memory store
(no PostgreSQL), with the same name-context credential shapes the other
model-input safety tests use; the non-secret marker beside them must arrive
(so the check is not vacuous). Written from the C1 interface without reading
the implementation; red until it lands.
"""

from __future__ import annotations

import json

import pytest

from tests.test_tool_expansion_c1_contract import (
    Harness,
    _cite_metrics,
    _todo_call,
    _tools,
)

MARKER = "todo-content-marker-5651"
SHAPES = {
    "userinfo": "dsn postgres://admin:sk-userinfo-5650@db.internal:5432/app",
    "assignment": "password=sk-assign-5651",
    "bearer": "Authorization: Bearer sk-bearer-5652",
    "scheme": "retried with Bearer sk-scheme-5653 after 401",
}
SECRETS = ("sk-userinfo-5650", "sk-assign-5651", "sk-bearer-5652", "sk-scheme-5653")


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_task_content_reaches_the_model_and_the_record_without_credentials(shape):
    content = f"{MARKER}: {SHAPES[shape]}"
    harness = Harness(
        [
            _tools(_todo_call([{"id": "t1", "content": content, "status": "pending"}])),
            _cite_metrics,
        ]
    )
    harness.run()

    # The tool messages of every later request: what the model is handed
    # back. (Its own assistant turn is replayed verbatim for pairing, as for
    # every tool; that is the model's output, not a new input.)
    returned = json.dumps(
        [
            m
            for call in harness.model.calls[1:]
            for m in call.messages
            if m.get("role") == "tool"
        ]
    )
    # Every stored tool result of the Run (opspilot_steps.tool_results).
    stored = json.dumps(harness.results())
    assert '"TodoWrite"' in stored and MARKER in stored, "the list was not stored"
    assert MARKER in returned, "the list never came back; the check is vacuous"
    for secret in SECRETS:
        assert secret not in returned, secret
        assert secret not in stored, secret
