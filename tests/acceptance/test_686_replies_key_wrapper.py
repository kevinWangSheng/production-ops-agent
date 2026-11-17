"""#686 contract r2 Q2 acceptance: a tool-free reply that is one JSON object
with a ``replies`` key but no ``schema_version`` key is read as the
``m0-response-v1`` wrapper.

Independent author, written without the implementation. External boundary
only: a published incident gets a continuation Run that reads a new human
message (tests.workbench_driver: real PG, product runner, scripted model),
and the assertions read the persisted Run state, terminal step, stored
replies and the projected ``IncidentOutcome``.

Origin: the #686 bounded real Run's continuation round and P1's own Run 1 both
returned ``{note, replies, report}`` without ``schema_version``; it was
rejected as a bare report, the Run handed off RESPONSE_INVALID and the reply
was lost. Unchanged by Q2: a ``schema_version`` present with any other value,
and a bare report (no ``replies`` key).
"""

import json
from uuid import UUID

import pytest

from tests.m1_investigation_support import reply, report_from_transcript, report_json
from tests.workbench_driver import (  # noqa: F401 -- shared pytest fixtures
    tool_round,
    workbench_database_fixture,
    workbench_fixture,
)

NOTE = "核对完毕。"
REVISED = "Revised: checkout errors are confirmed by the new sample."


def answer(seq, text="收到，结论如下。"):
    return {"to_input_seq": seq, "text": text, "evidence_ids": []}


def unversioned(*, replies, report, note=NOTE, **extra):
    """The shape the real Runs returned: wrapper members, no schema_version."""
    payload = {} if note is None else {"note": note}
    payload.update(extra, replies=replies, report=report)
    return reply(content=json.dumps(payload, ensure_ascii=False))


def revised_report(call):
    """A valid report citing the last view this Run delivered, new summary."""
    tool_views = [m for m in call.messages if m.get("role") == "tool"]
    evidence_id = json.loads(tool_views[-1]["content"])["evidence_id"]
    return json.loads(report_json(evidence_id=evidence_id, summary=REVISED))


def assert_conclusion_unchanged(workbench, conversation):
    after = workbench.incident(conversation.incident_id)
    for key in ("conclusion_bytes", "control_generation", "lifecycle", "mode"):
        assert after[key] == conversation.before[key], key


def stored_texts(workbench, conversation):
    return [
        item["text"]
        for item in workbench.replies_for(
            conversation.incident_id, conversation.input_seq
        )
    ]


def test_a_unversioned_wrapper_keeps_the_reply_and_publishes_the_report(workbench):
    scenario = workbench.published("686-q2-unversioned-with-report")
    conversation = workbench.continuation(scenario, "请更新结论。")
    expected = answer(conversation.input_seq)
    sent = []

    def final(call):
        report = revised_report(call)
        sent.append(report)
        return unversioned(replies=[expected], report=report)

    _, model = workbench.run(scenario, [tool_round(), final])

    assert len(model.calls) == 2, "an accepted ending needs no retry request"
    rows = workbench.store.rebuild(conversation.incident_id)
    assert rows["run"]["state"] == "completed"
    terminal = workbench.terminal(conversation.incident_id)
    assert terminal["execution"] == "completed"
    assert terminal["handoff"] is False
    assert "RESPONSE_INVALID" not in terminal["handoff_reasons"]
    assert not terminal["report_rejected_reason"]
    # the report member, not the whole object, is what was validated/published
    assert json.loads(terminal["report_content"]) == sent[0]
    assert (
        workbench.workbench.snapshot(conversation.incident_id)["report"]["summary"]
        == REVISED
    )
    assert stored_texts(workbench, conversation) == [expected["text"]]
    outcome = workbench.project(scenario)
    assert outcome.final_state == "completed"
    assert outcome.report_available is True


@pytest.mark.parametrize(
    ("mode", "note"),
    [
        pytest.param("report_optional", NOTE, id="report_optional-with-note"),
        pytest.param("report_optional", None, id="report_optional-replies-only"),
        pytest.param("reply_only", NOTE, id="reply_only-with-note"),
    ],
)
def test_b_unversioned_wrapper_with_null_report_answers(workbench, mode, note):
    scenario = workbench.published(f"686-q2-unversioned-null-{mode}")
    conversation = workbench.continuation(scenario, "你好", mode=mode)
    expected = answer(conversation.input_seq, "你好！现有结论不变。")

    _, model = workbench.run(
        scenario, [unversioned(replies=[expected], report=None, note=note)]
    )

    assert len(model.calls) == 1
    rows = workbench.store.rebuild(conversation.incident_id)
    assert rows["run"]["state"] == "completed", "a valid reply settles the Run"
    terminal = workbench.terminal(conversation.incident_id)
    assert terminal["execution"] == "answered"
    assert terminal["handoff"] is False
    assert "RESPONSE_INVALID" not in terminal["handoff_reasons"]
    # report null is "keep the earlier report", not a rejected update
    assert not terminal["report_rejected_reason"]
    assert terminal["report_content"] in (None, "")
    assert stored_texts(workbench, conversation) == [expected["text"]]
    assert_conclusion_unchanged(workbench, conversation)
    outcome = workbench.project(scenario)
    assert outcome.final_state == "answered"
    assert outcome.report_available is True


@pytest.mark.parametrize("version", ["m0-response-v2", "m0-report-v2"])
def test_c_other_schema_version_is_not_read_as_the_wrapper(workbench, version):
    """Before Q2 and after: the object is a (bad) bare report, so the reply
    inside it is not stored and the Run hands off RESPONSE_INVALID."""
    scenario = workbench.published(f"686-q2-other-version-{version}")
    conversation = workbench.continuation(scenario, "请更新结论。")
    expected = answer(conversation.input_seq)

    workbench.run(
        scenario,
        [unversioned(replies=[expected], report=None, schema_version=version)],
    )

    rows = workbench.store.rebuild(conversation.incident_id)
    assert rows["run"]["state"] == "waiting_human"
    terminal = workbench.terminal(conversation.incident_id)
    assert terminal["handoff"] is True
    assert terminal["handoff_reasons"] == ["RESPONSE_INVALID"]
    assert stored_texts(workbench, conversation) == []
    assert_conclusion_unchanged(workbench, conversation)


def test_d_bare_report_in_a_run_without_messages_is_accepted_as_before(workbench):
    scenario = workbench.from_alert("686-q2-bare-report")
    incident_id = UUID(scenario.subject_id)
    sent = []

    def bare(call):
        result = report_from_transcript(call)
        sent.append(result.content)
        return result

    _, model = workbench.run(scenario, [tool_round(), bare])

    assert len(model.calls) == 2
    rows = workbench.store.rebuild(incident_id)
    assert not [item for item in rows["inputs"] if item["kind"] == "message"]
    assert rows["run"]["state"] == "completed"
    terminal = workbench.terminal(incident_id)
    assert terminal["execution"] == "completed"
    assert terminal["handoff"] is False
    assert terminal["report_content"] == sent[0], "bare report bytes are kept"
    assert terminal["replies"] == []
    assert not terminal["report_rejected_reason"]
    outcome = workbench.project(scenario)
    assert outcome.final_state == "completed"
    assert outcome.report_available is True
