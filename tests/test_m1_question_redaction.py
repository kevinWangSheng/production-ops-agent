"""The Run's question reaches the model credential-redacted (M1-04 F9).

The question is human or alert free text; like a follow-up note
(``project_input_content``) it passes ``redact_credentials`` before it enters
the model-visible prefix (PRODUCT-CONSTRAINTS: credentials and secret-bearing
raw inputs must not enter prompts). The input snapshot keeps the raw text.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from opspilot.investigation import context as ctx
from opspilot.investigation.context import (
    ContextError,
    InvestigationInput,
    initial_messages,
    rebuild_transcript,
)
from opspilot.investigation.inputs import continuation_input
from opspilot.investigation.loop import InvestigationLoop
from opspilot.tools.fixture import FIXTURE_TARGET, fixture_face
from tests.m1_investigation_support import ScriptedModel, report_from_transcript
from tests.test_m1_investigation_compaction import _compacting_run
from tests.test_m1_investigation_context import Crash, _tool_rounds, _wide

# Same synthetic shapes the existing redaction tests use.
SECRETS = ("eyJhbGciOi.sk-secret-1", "hunter2", "AKIA1234SECRET", "tok_live_99")
QUESTION = (
    "checkout 5xx since 10:30; the probe used Authorization: Bearer "
    "eyJhbGciOi.sk-secret-1 against https://ops:hunter2@metrics.internal/api"
    "?api_key=AKIA1234SECRET&q=1 and token=tok_live_99 -- why is it failing?"
)


def _assert_redacted(text):
    for secret in SECRETS:
        assert secret not in text, (secret, text)
    assert text.startswith("checkout 5xx since 10:30; the probe used ")
    assert "metrics.internal/api" in text
    assert text.endswith(" -- why is it failing?")
    assert "[REDACTED_CREDENTIAL]" in text


def _transcript(store, request):
    return rebuild_transcript(
        store.snapshot(),
        run_id=request.run_id,
        authorized_targets=request.scope.target_ids,
        input=request.as_input(),
    )


def test_the_first_user_message_carries_the_question_redacted():
    loop, request, model, _, _, _ = _wide(
        replies=[*_tool_rounds(1), report_from_transcript]
    )
    request = replace(request, question=QUESTION)
    assert loop.run(request).execution == "completed"
    for call in model.calls:
        assert call.messages[1]["role"] == "user"
        _assert_redacted(call.messages[1]["content"])
        assert not any(s in str(call.messages) for s in SECRETS)
    # The input snapshot keeps the operator's raw question.
    assert request.as_input().question == QUESTION
    assert request.as_input().as_json()["question"] == QUESTION


def test_a_question_without_credentials_reaches_the_model_unchanged():
    loop, request, model, _, _, _ = _wide(
        replies=[*_tool_rounds(1), report_from_transcript]
    )
    question = (
        "checkout 5xx since 10:30 (author_filter=alice); error: connection refused"
    )
    request = replace(request, question=question)
    loop.run(request)
    assert model.calls[0].messages[1] == {"role": "user", "content": question}


def test_a_rebuilt_and_resumed_run_keeps_the_question_redacted():
    loop, request, model, _, store, _ = _wide(replies=[*_tool_rounds(1), Crash("x")])
    request = replace(request, question=QUESTION)
    with pytest.raises(Crash):
        loop.run(request)
    sent = list(model.calls[0].messages)
    transcript = _transcript(store, request)  # recorded hash check passes
    assert transcript.messages[: len(sent)] == sent
    _assert_redacted(transcript.messages[1]["content"])
    second = InvestigationLoop(
        model=ScriptedModel([report_from_transcript]),
        executor=loop.executor,
        store=store,
        clock=loop.clock,
    )
    assert second.resume(transcript).execution == "completed"
    resumed = second.model.calls[0].messages
    assert resumed[1] == sent[1]
    assert not any(s in str(resumed) for s in SECRETS)


def test_compaction_keeps_the_question_redacted():
    loop, request, model, _, store = _compacting_run()
    request = replace(request, question=QUESTION)
    outcome = loop.run(request)
    assert outcome.execution == "completed", outcome.handoff_reasons
    assert outcome.conclusion["conclusion"]["compactions"] == 1
    assert "ctx0:compact-1" in store.steps
    for call in model.calls:  # rounds, the compaction request, after it
        _assert_redacted(call.messages[1]["content"])
        assert not any(s in str(call.messages) for s in SECRETS)
    transcript = _transcript(store, request)
    assert transcript.segment == "ctx1"
    assert not any(s in str(transcript.messages) for s in SECRETS)


def test_a_continuation_question_is_redacted_and_keeps_its_handoff_note():
    face = fixture_face()
    old, new = uuid4(), uuid4()
    previous = face.input_for(
        run_id=str(old),
        question=QUESTION,
        target_id=FIXTURE_TARGET,
        deadline=datetime(2026, 9, 14, 1, 5, tzinfo=timezone.utc),
        model_requests=2,
    )
    successor = continuation_input(
        {
            "run": {"run_id": old, "input": previous.as_json()},
            "steps": [],
            "conclusion": None,
        },
        new_run_id=str(new),
        deadline=datetime(2026, 9, 14, 2, 0, tzinfo=timezone.utc),
        authorized_targets=frozenset({FIXTURE_TARGET}),
    )
    assert successor.question.startswith(QUESTION)  # snapshot stays raw
    messages, _ = initial_messages(
        successor, evidence_context=successor.evidence_context
    )
    content = messages[1]["content"]
    first, sep, note = content.partition("\n\n")
    assert sep and note == successor.question[len(QUESTION) + 2 :]
    _assert_redacted(first)
    # Round trip of the stored input does not change what the model sees.
    again = InvestigationInput.from_json(successor.as_json())
    assert initial_messages(again, evidence_context=again.evidence_context)[0] == (
        messages
    )


def test_a_run_recorded_with_the_raw_question_fails_its_rebuild_closed(monkeypatch):
    """A Run whose first round was sent before this redaction carries the
    raw-question hash; the rebuild cannot reproduce those bytes and refuses
    instead of resuming with a different prefix."""
    loop, request, _, _, store, _ = _wide(replies=[*_tool_rounds(1), Crash("x")])
    request = replace(request, question=QUESTION)
    with monkeypatch.context() as patched:
        patched.setattr(ctx, "redact_credentials", lambda text: text)
        with pytest.raises(Crash):
            loop.run(request)
    with pytest.raises(ContextError, match="INCOMPATIBLE_STATE"):
        _transcript(store, request)
