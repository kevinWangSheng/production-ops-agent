"""P1 independent acceptance; contract prereview A3–A6/B4/C1–C9.

Five functions of the redesign's eight-function cap. P2 extends this file;
it must use tests.workbench_driver rather than another support module.
A5/B5 recovery/replay use existing tests and real-Run evidence per section 5;
C4/C5/C9 prompt wording and version values are deliberately not asserted.
"""

import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from tests.m1_investigation_support import reply, report_from_transcript
from tests.workbench_driver import (  # noqa: F401 -- shared pytest fixtures
    UI_USER,
    step_contract,
    tool_round,
    workbench_database_fixture,
    workbench_fixture,
)


def one_observation_sample(workbench, incident_id):
    """Claim and submit one real Observer sample on the incident's session
    (A11 r4 review check); returns the receipt."""
    from opspilot.domain.evidence import QueryWindow
    from opspilot.domain.observation import HealthSample
    from opspilot.observation import ObservationStore
    from opspilot.persistence import PoolConfig

    with workbench.store.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_sessions SET active_sample_due_at="
            "clock_timestamp() WHERE incident_id=%s AND state='authorized'",
            (incident_id,),
        )
    observer = ObservationStore(
        workbench.store.dsn, pool=PoolConfig(min_size=0, max_size=2)
    )
    try:
        (lease,) = [
            item
            for item in observer.claim_due_samples(uuid4(), limit=10_000)
            if item.incident_id == incident_id
        ]
        assert observer.lease_scope_current(lease) is True
        now = observer.current_time()
        return observer.submit_sample(
            lease,
            HealthSample(
                sample_id=str(uuid4()),
                session_id=str(lease.session_id),
                sequence=lease.sequence,
                window=QueryWindow(
                    start=now - timedelta(minutes=5), end=now - timedelta(minutes=1)
                ),
                outcome="healthy",
                subject_control_generation=lease.subject_control_generation,
                observation_generation=lease.observation_generation,
                health_profile_revision=lease.health_profile_revision,
                required_signals_present=True,
            ),
            [],
        )
    finally:
        observer.close()


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


# P2 independent acceptance. The explicit P2 assignment allows five additional
# functions; the five P1 functions above are retained without changing them.
# All message/relay operations below go through the two driver accessors.


def admitted(result):
    assert 200 <= result.status < 300, (
        f"message must be admitted: HTTP {result.status} {result.text}"
    )


def current_run(driver, incident_id):
    return driver.store.rebuild(incident_id)["run"]


def last_message(driver, incident_id):
    return [row for row in driver.inputs(incident_id) if row["kind"] == "message"][-1]


def unchanged_by_message(before, after):
    for key in ("control_generation", "conclusion_bytes", "lifecycle", "mode", "state"):
        assert after["incident"][key] == before["incident"][key], key
    assert after["controls"] == before["controls"], "messages are not control actions"
    assert after["observation"] == before["observation"]


def conversation_input(driver, incident_id, *, floor, mode):
    config = current_run(driver, incident_id)["input"]["scope_facts"].get(
        "conversation", {}
    )
    assert config.get("floor") == floor, "each successor must refresh its read floor"
    assert config.get("mode") == mode


def model_inputs(call):
    for message in call.messages:
        try:
            content = json.loads(message.get("content") or "null")
        except (TypeError, ValueError):
            continue
        if isinstance(content, dict) and "investigation_inputs" in content:
            return content["investigation_inputs"]
    return []


def dictionaries(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from dictionaries(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from dictionaries(child)


@pytest.mark.parametrize(
    ("http_action", "continuation_control"),
    [("message", "pause"), ("follow_up", "cancel"), ("correct", "close")],
)
def test_p2_mid_run_message_reads_next_round_and_continuation_honors_control(
    workbench, http_action, continuation_control
):
    """A1/A2/A11, B1/B4/B7, D1, R5, O2/O4; no in-flight fencing."""
    scenario = workbench.from_alert(f"workbench-p2-mid-run-{http_action}")
    incident_id = UUID(scenario.subject_id)
    seq = max((row["sequence"] for row in workbench.inputs(incident_id)), default=0) + 1
    expected = answer(seq, "消息在下一轮读到，本轮回复已记录。")
    observed = {}

    def send_during_request(call):
        observed["before"] = workbench.records(incident_id)
        observed["post"] = workbench.message(
            incident_id, "请核对 payment 数据库。", action=http_action
        )
        observed["queued"] = workbench.records(incident_id)
        observed["unread"] = workbench.unread(incident_id)
        return tool_round(call_id="p2-before-message-read")

    def report_after_reply(call):
        observed["reading_steps"] = workbench.steps(incident_id)
        return report_from_transcript(call)

    workbench.run(
        scenario,
        [
            send_during_request,
            tool_round(
                content=json.dumps({"note": "核对消息。", "replies": [expected]}),
                call_id="p2-message-read",
            ),
            report_after_reply,
        ],
    )

    admitted(observed["post"])
    unchanged_by_message(observed["before"], observed["queued"])
    assert observed["queued"]["runs"] == observed["before"]["runs"], (
        "posting while running must preserve the Run and its live lease"
    )
    assert [row["sequence"] for row in observed["unread"]] == [seq]
    message = last_message(workbench, incident_id)
    assert message["actor"] == UI_USER
    assert message["content"]["channel"] == "investigation"
    assert message["sequence"] == seq
    reading = [
        step for step in observed["reading_steps"] if step_contract(step)["replies"]
    ]
    assert len(reading) == 1
    assert step_contract(reading[0])["watermark"] == seq
    assert_replies(step_contract(reading[0])["replies"], [expected])
    assert all(
        step_contract(step)["watermark"] < seq
        for step in observed["reading_steps"]
        if step["sequence"] < reading[0]["sequence"]
    ), "the request already in flight must not read the new message"
    assert workbench.unread(incident_id) == []
    corrections = [
        item
        for item in workbench.audit(incident_id).entries
        if item.category == "human_correction"
        and item.source == "opspilot_inputs"
        and item.record_id == str(message["input_id"])
    ]
    assert len(corrections) == 1, (
        "O4: human messages must be reconstructible from audit"
    )
    assert corrections[0].actor == UI_USER
    assert corrections[0].recorded_at == message["received_at"]

    workbench.watch(incident_id)
    before = workbench.records(incident_id)
    floor = max(row["input_watermark"] for row in before["runs"])
    admitted(workbench.message(incident_id, "继续说明。", action=http_action))
    continuation = current_run(workbench, incident_id)
    assert continuation["run_id"] != before["incident"]["current_run_id"]
    assert len(workbench.runs(incident_id)) == 2
    unchanged_by_message(before, workbench.records(incident_id))
    conversation_input(workbench, incident_id, floor=floor, mode="report_optional")
    assert incident_id in workbench.store.claimable_incidents(limit=10_000), (
        "A2: a queued continuation with a published conclusion must reach the worker"
    )
    continuation_seq = last_message(workbench, incident_id)["sequence"]

    def control_during_request(call):
        observed["running"] = current_run(workbench, incident_id)
        observed["control"] = workbench.control(incident_id, continuation_control)
        return response(replies=[answer(continuation_seq)])

    workbench.run(scenario, [control_during_request])
    assert observed["running"]["state"] == "running"
    admitted(observed["control"])
    after = workbench.incident(incident_id)
    assert after["conclusion_bytes"] == before["incident"]["conclusion_bytes"]
    assert after["mode"] == before["incident"]["mode"]
    assert incident_id not in workbench.store.claimable_incidents(limit=10_000)
    _, stopped_model = workbench.run(scenario, [])
    assert stopped_model.calls == [], "controlled continuation must not be claimed"
    assert current_run(workbench, incident_id)["state"] == (
        "paused" if continuation_control == "pause" else "cancelled"
    )
    if continuation_control == "pause":
        admitted(workbench.control(incident_id, "resume"))
        assert current_run(workbench, incident_id)["run_id"] == continuation["run_id"]
        workbench.run(scenario, [response(replies=[answer(continuation_seq)])])
        assert workbench.terminal(incident_id)["execution"] == "answered"
        # A11 r5 (review P2-1): closing a paused continuation lifts the pause,
        # and a later message still gets a reply_only continuation.
        admitted(workbench.message(incident_id, "再确认一次。"))
        admitted(workbench.control(incident_id, "pause"))
        assert workbench.incident(incident_id)["state"] == "paused"
        admitted(workbench.control(incident_id, "close"))
        closed = workbench.incident(incident_id)
        assert closed["state"] not in {"paused", "cancelled"}
        assert closed["conclusion_bytes"] == before["incident"]["conclusion_bytes"]
        admitted(workbench.message(incident_id, "关闭后补一句。"))
        conversation_input(
            workbench,
            incident_id,
            floor=max(row["input_watermark"] for row in workbench.runs(incident_id)),
            mode="reply_only",
        )
    else:
        assert after["state"] != "cancelled", "A11: only the continuation is cancelled"
        if continuation_control == "cancel":
            # A11 r4 + user grant 2026-11-17: cancel rebinds the session to the new generation
            def unbound(rows):
                return {
                    **rows,
                    "sessions": [
                        {
                            k: v
                            for k, v in s.items()
                            if k != "subject_control_generation"
                        }
                        for s in rows["sessions"]
                    ],
                }

            observed_after = workbench.observation(incident_id)
            assert unbound(observed_after) == unbound(before["observation"]), (
                "A11 r4: cancelling a continuation preserves recovery observation"
            )
            assert [
                s["subject_control_generation"] for s in observed_after["sessions"]
            ] == [workbench.incident(incident_id)["control_generation"]]
            # A11 r4 (review P1-1): a real sample after the cancel leaves the
            # session active and authorized (no binding_stale ending).
            one_observation_sample(workbench, incident_id)
            (session,) = workbench.observation(incident_id)["sessions"]
            assert (session["state"], session["ended_reason"]) == (
                "authorized",
                None,
            )
            assert session["authorized"] is True
            # Human control first (review P1-2): the message admitted before
            # the cancel opens nothing; a message after it does.
            runs_after_cancel = len(workbench.runs(incident_id))
            workbench.relay()
            assert len(workbench.runs(incident_id)) == runs_after_cancel
            admitted(workbench.message(incident_id, "取消后再问一次。"))
            assert len(workbench.runs(incident_id)) == runs_after_cancel + 1
            assert current_run(workbench, incident_id)["state"] == "queued"
        assert after["lifecycle"] == (
            "closed"
            if continuation_control == "close"
            else before["incident"]["lifecycle"]
        )
        if continuation_control == "close":
            admitted(workbench.message(incident_id, "关闭后还能解释吗？"))
            conversation_input(
                workbench,
                incident_id,
                floor=max(
                    row["input_watermark"] for row in workbench.runs(incident_id)
                ),
                mode="reply_only",
            )
            seq = last_message(workbench, incident_id)["sequence"]
            workbench.run(scenario, [response(replies=[answer(seq)])])
            assert workbench.incident(incident_id)["lifecycle"] == "closed"
            assert workbench.terminal(incident_id)["execution"] == "answered"


def test_p2_paused_message_stays_queued_until_resume(workbench):
    """D2/B7/R1: admission succeeds while human control prevents execution."""
    scenario = workbench.from_alert("workbench-p2-paused")
    incident_id = UUID(scenario.subject_id)
    admitted(workbench.control(incident_id, "pause"))
    before = workbench.records(incident_id)

    admitted(workbench.message(incident_id, "恢复后检查这条补充事实。"))
    seq = last_message(workbench, incident_id)["sequence"]
    workbench.relay()
    workbench.relay()

    after = workbench.records(incident_id)
    unchanged_by_message(before, after)
    assert after["runs"] == before["runs"]
    assert [row["sequence"] for row in workbench.unread(incident_id)] == [seq]
    _, model = workbench.run(scenario, [])
    assert model.calls == []
    assert workbench.replies_for(incident_id, seq) == []

    admitted(workbench.control(incident_id, "resume"))
    expected = answer(seq, "恢复后已读到补充事实。")

    def final_report(call):
        return response(
            replies=[expected], report=json.loads(report_from_transcript(call).content)
        )

    workbench.run(scenario, [tool_round(), final_report])
    assert len(workbench.runs(incident_id)) == 1
    assert current_run(workbench, incident_id)["state"] == "completed"
    assert workbench.unread(incident_id) == []
    assert_replies(workbench.replies_for(incident_id, seq), [expected])


@pytest.mark.parametrize(
    "state",
    ["resolved", "closed", "merged", "canceled", "failed", "human_owned", "no_run"],
)
def test_p2_message_admission_obeys_incident_state(workbench, state):
    """D5–D12: reply only, refuse, or record without investigating."""
    if state in {"resolved", "closed", "merged"}:
        scenario = workbench.published(f"workbench-p2-state-{state}")
    else:
        scenario = workbench.from_alert(
            f"workbench-p2-state-{state}",
            target=None if state == "no_run" else "checkout-prod",
        )
    incident_id = UUID(scenario.subject_id)
    late = {}
    if state == "resolved":
        workbench.resolved(incident_id)
    elif state == "closed":
        admitted(workbench.control(incident_id, "close"))
    elif state == "merged":
        survivor = workbench.from_alert("workbench-p2-merge-survivor")
        admitted(
            workbench.control(
                incident_id, "merge_into", into_incident_id=survivor.subject_id
            )
        )
    elif state == "canceled":
        admitted(workbench.control(incident_id, "cancel"))
    elif state == "human_owned":
        admitted(workbench.control(incident_id, "takeover"))
    elif state == "failed":

        def fail_after_freeze(call):
            late["post"] = workbench.message(incident_id, "尚未读取就失败的消息。")
            return reply(content="{}")

        # Post on the last, forced-final request: no subsequent request can
        # accidentally consume the message before the failed Run settles.
        workbench.run(scenario, [reply(content="{}"), fail_after_freeze])
        admitted(late["post"])
        assert workbench.incident(incident_id)["conclusion"] is None
        assert workbench.terminal(incident_id)["execution"] == "failed"
        assert len(workbench.unread(incident_id)) == 1
    before = workbench.records(incident_id)

    result = workbench.message(incident_id, f"状态 {state} 下的消息。")

    if state in {"merged", "canceled", "failed", "no_run"}:
        assert 400 <= result.status < 500, result.text
        assert workbench.records(incident_id) == before, (
            "refusal must not write business records"
        )
        if state in {"canceled", "failed"}:
            assert result.json().get("code") == "INVESTIGATION_NOT_AVAILABLE"
        elif state == "merged":
            assert result.json().get("code") == "MERGED"
        assert "message" not in workbench.actions(incident_id), (
            "composer must be unavailable"
        )
        if state == "failed":
            # D11/D12: the public Re-investigate action cancels + opens a
            # Run, refreshing B4 instead of inheriting a continuation floor.
            unread = workbench.unread(incident_id)[0]
            floor = max(row["input_watermark"] for row in before["runs"])
            admitted(workbench.control(incident_id, "reinvestigate"))
            conversation_input(
                workbench, incident_id, floor=floor, mode="report_required"
            )

            def finish_reinvestigation(call):
                return response(
                    replies=[answer(unread["sequence"], "重新调查读到了遗留消息。")],
                    report=json.loads(report_from_transcript(call).content),
                )

            _, model = workbench.run(scenario, [tool_round(), finish_reinvestigation])
            inputs = model_inputs(model.calls[0])
            assert any(
                item["sequence"] == unread["sequence"] and item.get("new")
                for item in inputs
            )
            assert workbench.unread(incident_id) == []
            assert workbench.replies_for(incident_id, unread["sequence"])
        return

    admitted(result)
    after = workbench.records(incident_id)
    unchanged_by_message(before, after)
    message = last_message(workbench, incident_id)
    assert message["actor"] == UI_USER
    if state == "human_owned":
        workbench.relay()
        workbench.relay()
        assert workbench.runs(incident_id) == before["runs"]
        assert workbench.incident(incident_id)["mode"] == "human_owned"
        assert workbench.inputs(incident_id)[-1] == message
        return

    assert len(after["runs"]) == len(before["runs"]) + 1
    conversation_input(
        workbench,
        incident_id,
        floor=max(row["input_watermark"] for row in before["runs"]),
        mode="reply_only",
    )
    expected = answer(message["sequence"], "仅解释已有结论。")
    _, model = workbench.run(scenario, [response(replies=[expected])])
    assert len(model.calls) == 1 and not model.calls[0].tools
    assert current_run(workbench, incident_id)["state"] == "completed"
    assert workbench.terminal(incident_id)["execution"] == "answered"
    assert not any(step["tool_results"] for step in workbench.steps(incident_id))
    assert (
        workbench.store.run_usage(current_run(workbench, incident_id)["run_id"])[
            "tool_operations_used"
        ]
        == 0
    )
    assert_replies(workbench.replies_for(incident_id, message["sequence"]), [expected])
    unchanged_by_message(before, workbench.records(incident_id))


@pytest.mark.parametrize("initial", ["published", "incomplete"])
def test_p2_idle_relay_is_idempotent_and_keeps_conversation_across_runs(
    workbench, initial
):
    """A10/B3/B4/B8, D3/D7a, R1–R5 including the r2 look-back."""
    scenario = workbench.from_alert(f"workbench-p2-relay-{initial}")
    incident_id = UUID(scenario.subject_id)

    def initial_report(call):
        report = json.loads(report_from_transcript(call).content)
        if initial == "incomplete":
            report.update(assessment_status="incomplete", conclusion="inconclusive")
        return reply(content=json.dumps(report))

    workbench.run(scenario, [tool_round(), initial_report])
    original = workbench.records(incident_id)
    if initial == "incomplete":
        assert original["incident"]["conclusion"] is None
        assert current_run(workbench, incident_id)["state"] == "waiting_human"
        assert workbench.terminal(incident_id)["handoff_reasons"] == [
            "INCOMPLETE_INVESTIGATION"
        ]
    for kind in ("follow_up", "correct"):
        workbench.legacy_input(incident_id, kind)
    legacy = [
        row
        for row in workbench.inputs(incident_id)
        if row["kind"] in {"follow_up", "correct"}
    ]
    workbench.relay()
    workbench.relay()
    assert len(workbench.runs(incident_id)) == 1, (
        "B3: historical input kinds are not unread messages"
    )
    assert workbench.unread(incident_id) == []

    key = f"p2-first-message-{incident_id}"
    admitted(workbench.message(incident_id, "先说明两个可选方案。", key=key))
    first_seq = last_message(workbench, incident_id)["sequence"]
    first_run = current_run(workbench, incident_id)
    admitted(workbench.message(incident_id, "先说明两个可选方案。", key=key))
    assert len(workbench.runs(incident_id)) == 2
    assert (
        len([row for row in workbench.inputs(incident_id) if row["kind"] == "message"])
        == 1
    )
    conversation_input(
        workbench,
        incident_id,
        floor=max(row["input_watermark"] for row in original["runs"]),
        mode="report_optional",
    )
    if initial == "incomplete":
        old_run = next(
            row
            for row in workbench.runs(incident_id)
            if row["run_id"] != first_run["run_id"]
        )
        assert old_run["state"] == "cancelled", (
            "A10: retire the inactive handoff before relay"
        )
    prior = answer(first_seq, f"可选方案一是等待，方案二是复核流量。{incident_id}")
    rejected_text = f"invalid-reply-{incident_id}"
    private_text = f"private-protocol-{incident_id}"
    observed = {}

    def reply_then_queue_second(call):
        observed["post"] = workbench.message(
            incident_id, "展开你刚才建议的第二个方案。"
        )
        observed["runs"] = workbench.runs(incident_id)
        return reply(
            content=json.dumps(
                {
                    "schema_version": "m0-response-v1",
                    "report": None,
                    "replies": [prior, answer(first_seq + 1000, rejected_text)],
                }
            ),
            reasoning=private_text,
        )

    workbench.run(scenario, [reply_then_queue_second])
    admitted(observed["post"])
    assert len(observed["runs"]) == 2, (
        "R5: a message after freeze waits for the next relay"
    )
    assert current_run(workbench, incident_id)["state"] == "completed"
    assert workbench.terminal(incident_id)["execution"] == "answered"
    second_seq = last_message(workbench, incident_id)["sequence"]
    assert [row["sequence"] for row in workbench.unread(incident_id)] == [second_seq]
    floor = max(row["input_watermark"] for row in workbench.runs(incident_id))

    workbench.relay()
    successor = current_run(workbench, incident_id)
    workbench.relay()
    assert len(workbench.runs(incident_id)) == 3, (
        "idle unread input opens exactly one successor"
    )
    assert current_run(workbench, incident_id)["run_id"] == successor["run_id"]
    assert successor["run_id"] != first_run["run_id"]
    conversation_input(workbench, incident_id, floor=floor, mode="report_optional")
    assert any(
        all(item.get(key) == value for key, value in prior.items())
        for item in dictionaries(successor["input"])
    ), "B8: the successor input must carry the prior validated reply"
    input_text = json.dumps(successor["input"], ensure_ascii=False)
    assert rejected_text not in input_text and private_text not in input_text

    _, model = workbench.run(
        scenario, [response(replies=[answer(second_seq, "第二个方案是复核流量。")])]
    )
    sent = json.dumps(model.calls[0].messages, ensure_ascii=False)
    assert prior["text"] in sent, "the next model request needs the previous answer"
    assert rejected_text not in sent and private_text not in sent
    inputs = model_inputs(model.calls[0])
    assert any(
        item["sequence"] == second_seq and item.get("new") is True for item in inputs
    )
    assert all(not item.get("new") for item in inputs if item["sequence"] <= floor)
    assert [
        row
        for row in workbench.inputs(incident_id)
        if row["kind"] in {"follow_up", "correct"}
    ] == legacy
    assert workbench.unread(incident_id) == []
    unchanged_by_message(original, workbench.records(incident_id))


@pytest.mark.parametrize(
    "phase", ["before_decision", "after_manual", "revise_during_watch"]
)
def test_p2_messages_preserve_recovery_authority_and_session(workbench, phase):
    """A7/D4: authorization follows the publishing Run, never a chat Run."""
    scenario = workbench.published(f"workbench-p2-watch-{phase}")
    incident_id = UUID(scenario.subject_id)
    workbench.enable_observation()
    if phase == "after_manual":
        admitted(
            workbench.control(
                incident_id, "register_remediation", revision="checkout:human-fix"
            )
        )
        assert (
            workbench.observation(incident_id)["sessions"][0]["authorization_source"]
            == "post_remediation"
        )
        workbench.expire_watch(incident_id)
    elif phase == "revise_during_watch":
        workbench.automatic_observation()
        assert len(workbench.observation(incident_id)["sessions"]) == 1
    before = workbench.records(incident_id)
    report = json.loads(workbench.terminal(incident_id)["report_content"])

    admitted(workbench.message(incident_id, "说明当前结论，并按需要更新报告。"))
    seq = last_message(workbench, incident_id)["sequence"]
    assert current_run(workbench, incident_id)["state"] == "queued"
    unchanged_by_message(before, workbench.records(incident_id))
    if phase == "before_decision":
        # The first conclusion qualifies even while the chat Run is queued.
        workbench.automatic_observation()
        watching = workbench.observation(incident_id)
        assert len(watching["sessions"]) == 1
        assert watching["sessions"][0]["state"] == "authorized"
        assert watching["sessions"][0]["authorization_source"] == "automatic"
        assert (
            len(
                [
                    row
                    for row in watching["authorizations"]
                    if row["state"] == "authorized"
                ]
            )
            == 1
        )
    else:
        watching = before["observation"]

    if phase == "revise_during_watch":
        report["summary"] = f"续接调查修订后的结论 {incident_id}"
    else:
        report = None
    workbench.run(
        scenario, [response(replies=[answer(seq, "结论已说明。")], report=report)]
    )
    workbench.automatic_observation()
    workbench.automatic_observation()

    after = workbench.records(incident_id)
    assert (
        after["incident"]["control_generation"]
        == before["incident"]["control_generation"]
    )
    assert after["incident"]["mode"] == before["incident"]["mode"]
    assert after["incident"]["lifecycle"] == "observing_recovery"
    assert_replies(
        workbench.replies_for(incident_id, seq), [answer(seq, "结论已说明。")]
    )
    assert after["observation"]["sessions"] == watching["sessions"], (
        "the same observation session must survive"
    )
    assert after["observation"]["samples"] == watching["samples"]
    assert after["observation"]["endings"] == watching["endings"]
    if phase == "after_manual":
        assert not [
            row
            for row in after["observation"]["authorizations"]
            if row["state"] == "authorized"
        ]
        assert not [
            row
            for row in after["controls"]
            if row["action"] == "auto_authorize_observation"
        ]
    else:
        assert after["observation"]["authorizations"] == watching["authorizations"]
        assert (
            len(
                [
                    row
                    for row in after["controls"]
                    if row["action"] == "auto_authorize_observation"
                ]
            )
            == 1
        )
    if phase == "revise_during_watch":
        assert workbench.terminal(incident_id)["execution"] == "completed"
        assert (
            workbench.workbench.snapshot(incident_id)["report"]["summary"]
            == report["summary"]
        )
        assert (
            after["incident"]["conclusion_bytes"]
            != before["incident"]["conclusion_bytes"]
        )
    else:
        assert workbench.terminal(incident_id)["execution"] == "answered"
        assert (
            after["incident"]["conclusion_bytes"]
            == before["incident"]["conclusion_bytes"]
        )
