"""P1 independent acceptance; contract prereview A3–A6/B4/C1–C9.

Five functions of the redesign's eight-function cap. P2 extends this file;
it must use tests.workbench_driver rather than another support module.
A5/B5 recovery/replay use existing tests and real-Run evidence per section 5;
C4/C5/C9 prompt wording and version values are deliberately not asserted.
"""

import json
from uuid import UUID

import pytest

from tests.m1_investigation_support import reply, report_from_transcript
from tests.workbench_driver import (  # noqa: F401 -- shared pytest fixtures
    step_contract,
    tool_round,
    workbench_database_fixture,
    workbench_fixture,
)


def response(*, replies, report=None):
    return reply(
        content=json.dumps(
            {
                "schema_version": "m0-response-v1",
                "replies": replies,
                "report": report,
            },
            ensure_ascii=False,
        )
    )


def answer(seq, text="你好！", *, evidence_ids=()):
    return {"to_input_seq": seq, "text": text, "evidence_ids": list(evidence_ids)}


def assert_preserved(driver, conversation):
    after = driver.incident(conversation.incident_id)
    for key in ("conclusion_bytes", "control_generation", "lifecycle", "mode"):
        assert after[key] == conversation.before[key], key
    assert (
        driver.observation(conversation.incident_id) == conversation.observation_before
    )


def assert_replies(actual, expected):
    # Compare contract fields and exact reply count; permit extra metadata.
    assert len(actual) == len(expected)
    for item, wanted in zip(actual, expected, strict=True):
        for key, value in wanted.items():
            assert item.get(key) == value, key


@pytest.mark.parametrize("mode", ["report_optional", "reply_only"])
def test_scenario2_greeting_answers_once_without_changing_conclusion_or_watch(
    workbench, mode
):
    scenario = workbench.published(f"workbench-p1-greeting-{mode}")
    incident_id = UUID(scenario.subject_id)
    workbench.watch(incident_id)
    conversation = workbench.continuation(scenario, "你好", mode=mode)
    expected = answer(conversation.input_seq)

    _, model = workbench.run(scenario, [response(replies=[expected])])

    assert len(model.calls) == 1, (
        "a greeting on a continuation Run needs exactly one model request"
    )
    rows = workbench.store.rebuild(incident_id)
    assert rows["run"]["state"] == "completed", "a direct reply must settle the Run"
    terminal = workbench.terminal(incident_id)
    assert terminal["execution"] == "answered"
    assert terminal["handoff"] is False
    assert terminal["report_content"] in (None, "")
    assert workbench.store.run_usage(conversation.run_id)["tool_operations_used"] == 0
    assert not any(step["tool_results"] for step in workbench.steps(incident_id))
    assert not any(
        step_contract(step)["assistant"].get("tool_calls")
        for step in workbench.steps(incident_id)
    )
    if mode == "reply_only":
        assert not model.calls[0].tools
    assert_preserved(workbench, conversation)
    assert_replies(
        workbench.replies_for(incident_id, conversation.input_seq), [expected]
    )
    outcome = workbench.project(scenario)
    assert outcome.final_state == "answered", "acceptance.py must recognise answered"
    assert outcome.report_available is True


@pytest.mark.parametrize(
    "parseable", [True, False], ids=["json-reply", "plain-narration"]
)
def test_scenario2b_reply_belongs_to_the_step_that_read_the_message(
    workbench, parseable
):
    scenario = workbench.published("workbench-p1-step-reply")
    conversation = workbench.continuation(scenario, "请说明现有结论。")
    seq = conversation.input_seq
    old_report = workbench.workbench.snapshot(conversation.incident_id)["report"]
    inherited_id = old_report["facts"][0]["evidence_ids"][0]
    early = answer(
        seq, "我先说明现有证据。", evidence_ids=[inherited_id, "not-delivered"]
    )
    visible = (
        json.dumps(
            {
                "note": "正在核对现有结论。",
                "replies": [
                    early,
                    answer(conversation.floor, "This is not a new message."),
                    answer(seq + 1000, "This input has not been read."),
                    {"to_input_seq": seq, "text": "Missing evidence_ids."},
                ],
            },
            ensure_ascii=False,
        )
        if parseable
        else "正在核对现有结论；这不是 JSON。"
    )
    final = answer(seq, "现有结论保持不变。")
    # The first reply must already be durable when the provider receives the
    # next request. This checks visibility at an external boundary, not an
    # internal method sequence; keep assertions outside the scripted client.
    observed = []

    def final_output(call):
        observed.extend(workbench.steps(conversation.incident_id))
        return response(replies=[final])

    result, _ = workbench.run(scenario, [tool_round(content=visible), final_output])

    matching = [
        s for s in observed if step_contract(s)["assistant"].get("content") == visible
    ]
    assert len(matching) == 1, (
        f"the reading step must be durable before the final output; {result}"
    )
    (step,) = matching
    fields = step_contract(step)
    assert fields["watermark"] == seq
    if parseable:
        assert_replies(
            fields["replies"],
            [
                {
                    **early,
                    "evidence_ids": [inherited_id],
                    "unsupported": True,
                }
            ],
        )
        assert fields["note"] == "正在核对现有结论。"
    else:
        assert fields["replies"] == []
        assert fields["note"] == visible, "unparseable content is ordinary narration"
    rows = workbench.store.rebuild(conversation.incident_id)
    assert rows["run"]["state"] == "completed"
    assert workbench.terminal(conversation.incident_id)["execution"] == "answered"
    replies = workbench.replies_for(conversation.incident_id, seq)
    assert [item["text"] for item in replies] == (
        [early["text"], final["text"]] if parseable else [final["text"]]
    )
    assert_preserved(workbench, conversation)


@pytest.mark.parametrize("mode", ["report_optional", "reply_only"])
def test_scenario3a_valid_reply_survives_a_rejected_report(workbench, mode):
    scenario = workbench.published(f"workbench-p1-report-rejected-{mode}")
    prior_report = json.loads(
        workbench.terminal(UUID(scenario.subject_id))["report_content"]
    )
    conversation = workbench.continuation(scenario, "请更新结论。", mode=mode)
    expected = answer(conversation.input_seq, "回复保留，旧结论仍有效。")
    invalid_report = {
        "schema_version": "m0-report-v2",
        "summary": "Missing required report fields.",
    }

    # reply_only must reject even a report that was already valid/published.
    offered_report = prior_report if mode == "reply_only" else invalid_report
    workbench.run(scenario, [response(replies=[expected], report=offered_report)])

    rows = workbench.store.rebuild(conversation.incident_id)
    assert rows["run"]["state"] == "completed", (
        "a rejected report must not discard valid replies"
    )
    terminal = workbench.terminal(conversation.incident_id)
    assert terminal["execution"] == "answered"
    assert terminal["handoff"] is False
    assert terminal["report_rejected_reason"], (
        "the rejected update must have a persisted reason"
    )
    if mode == "reply_only":
        assert terminal["report_rejected_reason"] == "REPLY_ONLY"
    assert_replies(
        workbench.replies_for(conversation.incident_id, conversation.input_seq),
        [expected],
    )
    assert_preserved(workbench, conversation)
    outcome = workbench.project(scenario)
    assert outcome.final_state == "answered"
    assert outcome.report_available is True


def test_scenario3b_invalid_reply_and_report_hand_off_without_erasing_conclusion(
    workbench,
):
    scenario = workbench.published("workbench-p1-response-invalid")
    conversation = workbench.continuation(scenario, "请更新结论。")
    invalid_replies = [answer(conversation.input_seq + 1000, "Not a read message.")]

    workbench.run(
        scenario,
        [response(replies=invalid_replies, report={"summary": "Invalid report."})],
    )

    assert (
        workbench.store.rebuild(conversation.incident_id)["run"]["state"]
        == "waiting_human"
    )
    terminal = workbench.terminal(conversation.incident_id)
    assert terminal["handoff_reasons"] == ["RESPONSE_INVALID"]
    assert terminal["handoff"] is True
    assert workbench.replies_for(conversation.incident_id, conversation.input_seq) == []
    assert_preserved(workbench, conversation)


def test_back_compat_alert_without_messages_publishes_the_bare_report(workbench):
    scenario = workbench.from_alert("workbench-p1-bare-report")
    incident_id = UUID(scenario.subject_id)
    sent = []

    def bare_report(call):
        result = report_from_transcript(call)
        sent.append(result.content)
        return result

    workbench.run(scenario, [tool_round(), bare_report])

    rows = workbench.store.rebuild(incident_id)
    assert not [item for item in rows["inputs"] if item["kind"] == "message"]
    assert rows["run"]["state"] == "completed"
    assert rows["conclusion"] is not None
    terminal = workbench.terminal(incident_id)
    assert terminal["execution"] == "completed"
    assert terminal["handoff"] is False
    assert terminal["report_content"] == sent[0], (
        "C6 preserves the bare report's exact bytes"
    )
    assert terminal["replies"] == []
    assert not terminal["report_rejected_reason"]
    assert (
        workbench.workbench.snapshot(incident_id)["report"]["summary"]
        == json.loads(sent[0])["summary"]
    )
    outcome = workbench.project(scenario)
    assert outcome.final_state == "completed"
    assert outcome.report_available is True
