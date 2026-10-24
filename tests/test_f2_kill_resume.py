"""Deterministic checks of the F2 D kill/resume harness (no model, no PG)."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from scripts import f2_kill_resume as h

STEP1 = "11111111-1111-1111-1111-111111111111"
STEP2 = "22222222-2222-2222-2222-222222222222"
LEASE = "2026-10-22T20:07:00+00:00"


def _step(
    step_id,
    seq,
    *,
    tools=(),
    results=0,
    ordinals=None,
    status="tool_result_committed",
):
    step = {
        "step_id": step_id,
        "sequence": seq,
        "logical_key": f"ctx0:round-{seq + 1}",
        "status": status,
        "kind": None,
        "tool_calls": list(tools),
        "tool_results": results,
        "observed_at": "2026-10-22T20:00:00+00:00",
    }
    # Ledgers frozen before #332 have only the count.
    if ordinals is not None:
        step["tool_ordinals"] = list(ordinals)
        step["tool_results"] = len(ordinals)
    return step


def _snap(
    *,
    epoch=1,
    state="running",
    steps=(),
    reservations=(),
    charges=(),
    evidence=(),
    cited=None,
    lease=LEASE,
    **run,
):
    return {
        "db_now": "2026-10-22T20:00:00+00:00",
        "run": {
            "state": state,
            "epoch": epoch,
            "owner": "o",
            "lease_until": lease,
            "budget_reserved": 0,
            "budget_spent": 0,
            "budget_unknown": 0,
            "tool_operations_used": len(charges),
            "tool_seconds_used": 0.0,
            **run,
        },
        "steps": list(steps),
        "reservations": list(reservations),
        "charges": list(charges),
        "evidence": list(evidence),
        "incident_state": "open",
        "conclusion_evidence_ids": cited,
    }


def _res(rid, state="spent"):
    return {
        "reservation_id": rid,
        "state": state,
        "reserved_seconds": 360.0,
        "seconds": None if state == "reserved" else 5.0,
    }


def _charge(op, epoch=1, seconds=1.0, dispatch=None):
    return {
        "dispatch_id": dispatch or f"d-{op}-{epoch}",
        "epoch": epoch,
        "operation_id": op,
        "reserved": 30.0,
        "seconds": seconds,
    }


EV1 = {"evidence_id": f"{STEP1}-t0:x", "committed": True, "raw_sha256": "aa"}


def test_tool_names_reads_both_committed_shapes():
    call = {"function": {"name": "metrics_range_query"}}
    assert h.tool_names({"assistant": {"tool_calls": [call]}}) == [
        "metrics_range_query"
    ]
    assert h.tool_names(
        {"tool_calls": [call, {"function": {"name": "traces_search"}}]}
    ) == [
        "metrics_range_query",
        "traces_search",
    ]
    assert h.tool_names({"assistant": {"content": "x"}}) == []
    assert h.tool_names(None) == []


def test_conclusion_evidence_ids_reads_the_published_row():
    assert h.conclusion_evidence_ids(
        {"kind": "conclusion", "conclusion": {"evidence_ids": ["a"]}}
    ) == ["a"]
    assert h.conclusion_evidence_ids(None) is None
    assert h.conclusion_evidence_ids({"conclusion": {}}) is None


def test_operation_tool_maps_operation_ids_to_their_step_call():
    snap = _snap(
        steps=[_step(STEP1, 0, tools=["traces_search", "metrics_range_query"])]
    )
    assert h.operation_tool(snap, f"{STEP1}-t1") == "metrics_range_query"
    assert h.operation_tool(snap, f"{STEP1}-t0") == "traces_search"
    assert h.operation_tool(snap, f"{STEP1}-t9") is None
    assert h.operation_tool(snap, "garbage") is None


def test_model_in_flight_needs_exactly_one_unsettled_reservation():
    assert (
        h.model_in_flight(_snap(reservations=[_res("r1"), _res("r2", "reserved")]))
        == "r2"
    )
    assert h.model_in_flight(_snap(reservations=[_res("r1")])) is None
    two = _snap(reservations=[_res("r1", "reserved"), _res("r2", "reserved")])
    assert h.model_in_flight(two) is None


def test_pending_step_is_the_last_step_with_undispatched_tools():
    planned = _step(
        STEP2, 1, tools=["metrics_range_query"], status="response_committed"
    )
    base = [_step(STEP1, 0, tools=["traces_search"], results=1)]
    assert h.pending_step(_snap(steps=[*base, planned]))["step_id"] == STEP2
    charged = _snap(
        steps=[*base, planned], charges=[_charge(f"{STEP2}-t0", seconds=None)]
    )
    assert h.pending_step(charged) is None
    no_tools = _step(STEP2, 1, status="response_committed")
    assert h.pending_step(_snap(steps=[*base, no_tools])) is None
    late = dict(planned, status="late_result")
    assert h.pending_step(_snap(steps=[*base, late])) is None


def test_stalled_dispatch_is_the_only_unsettled_query_of_the_epoch():
    steps = [_step(STEP1, 0, tools=["metrics_range_query", "traces_search"])]
    stalled = _charge(f"{STEP1}-t0", seconds=None)
    other = _charge(f"{STEP1}-t1", seconds=None)
    assert h.stalled_dispatch(_snap(steps=steps, charges=[stalled])) == stalled
    assert h.stalled_dispatch(_snap(steps=steps, charges=[stalled, other])) is None
    assert h.stalled_dispatch(_snap(steps=steps, charges=[other])) is None
    old_epoch = _snap(epoch=2, steps=steps, charges=[stalled])
    assert h.stalled_dispatch(old_epoch) is None


def _model_case():
    steps = [_step(STEP1, 0, tools=["traces_search"], results=1)]
    pre = _snap(
        steps=steps,
        reservations=[_res("r1"), _res("r2", "reserved")],
        charges=[_charge(f"{STEP1}-t0")],
        evidence=[EV1],
        budget_reserved=1,
    )
    post = copy.deepcopy(pre)
    final = _snap(
        epoch=2,
        state="completed",
        steps=[
            *steps,
            _step(STEP2, 1),
            _step("33333333-3333-3333-3333-333333333333", 2),
            # The published conclusion record: no model request of its own
            # (first live run, 2026-10-23, counted it and read 0 re-sends).
            dict(
                _step("44444444-4444-4444-4444-444444444444", 3),
                kind="conclusion",
                logical_key="conclusion:g0:ctx0:round-3",
            ),
        ],
        reservations=[_res("r1"), _res("r2", "reserved"), _res("r3"), _res("r4")],
        charges=[_charge(f"{STEP1}-t0")],
        evidence=[EV1],
        cited=[EV1["evidence_id"]],
        budget_reserved=1,
    )
    return pre, post, final


def test_model_in_flight_verdicts_pass_on_one_resend_and_a_kept_reservation():
    pre, post, final = _model_case()
    result = h.verdicts(
        "model_in_flight",
        pre=pre,
        post=post,
        final=final,
        epoch_started_at="2026-10-22T20:07:01.5+00:00",
        poll_seconds=2.0,
    )
    c = result["criteria"]
    assert c["breakpoint_held"]["passed"]
    assert c["model_resend"]["resent"] == 1 and c["model_resend"]["passed"]
    assert c["tool_redispatch"]["redispatched"] == [] and c["tool_redispatch"]["passed"]
    assert c["evidence_kept"]["passed"]
    assert c["unsettled_cost_kept"]["still_reserved_at_end"] == ["r2"]
    assert c["new_epoch_timely"]["seconds_after_lease_expiry"] == pytest.approx(1.5)
    assert result["passed"]


def test_a_response_landing_between_check_and_kill_is_not_the_breakpoint():
    pre, post, final = _model_case()
    post["reservations"] = [_res("r1"), _res("r2")]
    post["steps"].append(_step(STEP2, 1))
    held, _ = h.breakpoint_held("model_in_flight", pre, post)
    assert not held


def test_a_second_lost_request_fails_the_resend_bound():
    pre, post, final = _model_case()
    final["reservations"].append(_res("r5", "reserved"))
    result = h.verdicts(
        "model_in_flight",
        pre=pre,
        post=post,
        final=final,
        epoch_started_at="2026-10-22T20:07:01+00:00",
        poll_seconds=2.0,
    )
    assert result["criteria"]["model_resend"]["resent"] == 2
    assert not result["passed"]


def test_late_epoch_and_lost_evidence_fail():
    pre, post, final = _model_case()
    late = h.verdicts(
        "model_in_flight",
        pre=pre,
        post=post,
        final=final,
        epoch_started_at="2026-10-22T20:07:02.3+00:00",
        poll_seconds=2.0,
    )
    assert not late["criteria"]["new_epoch_timely"]["passed"] and not late["passed"]
    final["evidence"] = [dict(EV1, raw_sha256="bb")]
    changed = h.verdicts(
        "model_in_flight",
        pre=pre,
        post=post,
        final=final,
        epoch_started_at="2026-10-22T20:07:01+00:00",
        poll_seconds=2.0,
    )
    assert changed["criteria"]["evidence_kept"]["missing_or_changed"] == [
        EV1["evidence_id"]
    ]
    final["evidence"] = [EV1]
    final["conclusion_evidence_ids"] = None
    uncited = h.verdicts(
        "model_in_flight",
        pre=pre,
        post=post,
        final=final,
        epoch_started_at="2026-10-22T20:07:01+00:00",
        poll_seconds=2.0,
    )
    assert not uncited["criteria"]["evidence_kept"]["passed"]


def test_response_committed_verdicts_count_each_pending_tool_once():
    first = _step(STEP1, 0, tools=["traces_search"], results=1)
    planned = _step(
        STEP2,
        1,
        tools=["metrics_range_query", "traces_search"],
        status="response_committed",
    )
    pre = _snap(
        steps=[first, planned],
        reservations=[_res("r1"), _res("r2")],
        charges=[_charge(f"{STEP1}-t0")],
        evidence=[EV1],
    )
    post = copy.deepcopy(pre)
    done = dict(planned, results=2, status="tool_result_committed")
    final = _snap(
        epoch=2,
        state="completed",
        steps=[first, done, _step("33333333-3333-3333-3333-333333333333", 2)],
        reservations=[_res("r1"), _res("r2"), _res("r3")],
        charges=[
            _charge(f"{STEP1}-t0"),
            _charge(f"{STEP2}-t0", 2),
            _charge(f"{STEP2}-t1", 2),
        ],
        evidence=[EV1],
        cited=[EV1["evidence_id"], "other"],
    )
    result = h.verdicts(
        "response_committed",
        pre=pre,
        post=post,
        final=final,
        epoch_started_at="2026-10-22T20:07:00+00:00",
        poll_seconds=2.0,
    )
    c = result["criteria"]
    assert c["breakpoint_held"]["passed"] and c["breakpoint_held"]["step_id"] == STEP2
    assert c["tool_redispatch"]["pending_dispatches"] == {
        f"{STEP2}-t0": 1,
        f"{STEP2}-t1": 1,
    }
    assert c["model_resend"]["resent"] == 0
    assert c["unsettled_cost_kept"]["kind"] == "none_at_kill"
    assert result["passed"]
    final["charges"].append(_charge(f"{STEP2}-t0", 2, dispatch="again"))
    twice = h.verdicts(
        "response_committed",
        pre=pre,
        post=post,
        final=final,
        epoch_started_at="2026-10-22T20:07:00+00:00",
        poll_seconds=2.0,
    )
    assert not twice["criteria"]["tool_redispatch"]["passed"]


def test_tool_in_flight_verdicts_allow_exactly_the_stalled_query_again():
    first = _step(STEP1, 0, tools=["traces_search"], results=1)
    planned = _step(
        STEP2, 1, tools=["metrics_range_query"], status="response_committed"
    )
    stalled = _charge(f"{STEP2}-t0", seconds=None, dispatch="stalled")
    pre = _snap(
        steps=[first, planned],
        reservations=[_res("r1"), _res("r2")],
        charges=[_charge(f"{STEP1}-t0"), stalled],
        evidence=[EV1],
    )
    post = copy.deepcopy(pre)
    final = _snap(
        epoch=2,
        state="completed",
        steps=[
            first,
            dict(planned, results=1),
            _step("33333333-3333-3333-3333-333333333333", 2),
        ],
        reservations=[_res("r1"), _res("r2"), _res("r3")],
        charges=[_charge(f"{STEP1}-t0"), stalled, _charge(f"{STEP2}-t0", 2)],
        evidence=[EV1],
        cited=[EV1["evidence_id"]],
    )
    result = h.verdicts(
        "tool_in_flight",
        pre=pre,
        post=post,
        final=final,
        epoch_started_at="2026-10-22T20:07:00+00:00",
        poll_seconds=2.0,
    )
    c = result["criteria"]
    assert c["tool_redispatch"]["redispatched"] == [f"{STEP2}-t0"]
    assert c["unsettled_cost_kept"]["still_unsettled_at_end"] == ["stalled"]
    assert result["passed"]
    settled = copy.deepcopy(final)
    settled["charges"][1]["seconds"] = 30.0
    released = h.verdicts(
        "tool_in_flight",
        pre=pre,
        post=post,
        final=settled,
        epoch_started_at="2026-10-22T20:07:00+00:00",
        poll_seconds=2.0,
    )
    assert not released["criteria"]["unsettled_cost_kept"]["passed"]
    rerun = copy.deepcopy(final)
    rerun["charges"].append(_charge(f"{STEP1}-t0", 2))
    double = h.verdicts(
        "tool_in_flight",
        pre=pre,
        post=post,
        final=rerun,
        epoch_started_at="2026-10-22T20:07:00+00:00",
        poll_seconds=2.0,
    )
    assert not double["criteria"]["tool_redispatch"]["passed"]


def test_check_owned_worker_refuses_anything_but_our_child_worker():
    me = 4242
    good = f"901 {me} /repo/.venv/bin/python -m opspilot.worker_main"
    h.check_owned_worker(good, pid=901, parent=me)
    with pytest.raises(RuntimeError, match="PID_NOT_OURS"):
        h.check_owned_worker(
            "901 1 /repo/.venv/bin/python -m opspilot.worker_main", pid=901, parent=me
        )
    with pytest.raises(RuntimeError, match="PID_NOT_WORKER"):
        h.check_owned_worker(
            f"901 {me} /repo/.venv/bin/python -m opspilot.web serve", pid=901, parent=me
        )
    with pytest.raises(RuntimeError, match="PID_MISMATCH"):
        h.check_owned_worker(good, pid=902, parent=me)
    with pytest.raises(RuntimeError, match="PID_MISMATCH"):
        h.check_owned_worker(f"1 {me} python -m opspilot.worker_main", pid=1, parent=me)
    with pytest.raises(RuntimeError, match="PID_PROTECTED"):
        h.check_owned_worker(
            f"901 {me} verge-mihomo -m opspilot.worker_main", pid=901, parent=me
        )
    with pytest.raises(RuntimeError, match="PID_UNVERIFIED"):
        h.check_owned_worker("", pid=901, parent=me)


def test_env_helpers_keep_only_path_home_and_declared_values(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "should-not-leak")
    monkeypatch.setenv("PATH", "/bin")
    env = h.minimal_env({"OPSPILOT_TOOL_PROFILE": "otel-demo"})
    assert "DEEPSEEK_API_KEY" not in env and env["PATH"] == "/bin"
    file = tmp_path / "x.env"
    file.write_text("# c\nexport A='1'\nB=\"two\"\nC=3\n\nbad line\n")
    assert h.read_env_file(file) == {"A": "1", "B": "two", "C": "3"}


def test_a_settled_but_uncommitted_sibling_counts_as_a_second_redispatch():
    # Live finding 2026-10-23: with two calls in flight, the sibling settles
    # but its result waits behind the stalled call (#309 index-order commit),
    # so the next epoch runs both again.
    first = _step(STEP1, 0, tools=["traces_search"], results=1)
    planned = _step(
        STEP2,
        1,
        tools=["metrics_range_query", "metrics_range_query"],
        status="response_committed",
    )
    stalled = _charge(f"{STEP2}-t0", seconds=None, dispatch="stalled")
    sibling = _charge(f"{STEP2}-t1", seconds=1.0, dispatch="sibling")
    pre = _snap(
        steps=[first, planned],
        reservations=[_res("r1"), _res("r2")],
        charges=[_charge(f"{STEP1}-t0"), stalled, sibling],
        evidence=[EV1],
    )
    post = copy.deepcopy(pre)
    final = _snap(
        epoch=2,
        state="completed",
        steps=[first, dict(planned, results=2)],
        reservations=[_res("r1"), _res("r2")],
        charges=[
            *pre["charges"],
            _charge(f"{STEP2}-t0", 2),
            _charge(f"{STEP2}-t1", 2),
        ],
        evidence=[EV1],
        cited=[EV1["evidence_id"]],
    )
    result = h.verdicts(
        "tool_in_flight",
        pre=pre,
        post=post,
        final=final,
        epoch_started_at="2026-10-22T20:07:00+00:00",
        poll_seconds=2.0,
    )
    tool = result["criteria"]["tool_redispatch"]
    assert tool["redispatched"] == [f"{STEP2}-t0", f"{STEP2}-t1"]
    assert tool["settled_uncommitted_at_kill"] == [f"{STEP2}-t1"]
    assert not tool["passed"] and not result["passed"]


def test_a_kept_tool_error_need_not_be_cited_but_must_stay_unchanged():
    pre, post, final = _model_case()
    error = {
        "evidence_id": f"{STEP1}-t1:y",
        "committed": True,
        "adopted": False,
        "raw_sha256": "ee",
    }
    for snap in (pre, post, final):
        snap["evidence"].append(dict(error))
    kwargs = dict(epoch_started_at="2026-10-22T20:07:01+00:00", poll_seconds=2.0)
    ok = h.verdicts("model_in_flight", pre=pre, post=post, final=final, **kwargs)
    assert ok["criteria"]["evidence_kept"]["passed"]
    assert ok["criteria"]["evidence_kept"]["not_adopted_before_kill"] == [
        error["evidence_id"]
    ]
    final["evidence"][-1]["raw_sha256"] = "ff"
    changed = h.verdicts("model_in_flight", pre=pre, post=post, final=final, **kwargs)
    assert not changed["criteria"]["evidence_kept"]["passed"]


# -- #332 U-3: siblings committed out of order ---------------------------------


def _parallel_step(ordinals):
    return _step(
        STEP2,
        1,
        tools=["traces_search", "metrics_range_query"],
        ordinals=ordinals,
        status="tool_result_committed" if ordinals else "response_committed",
    )


def test_committed_ops_reads_actual_ordinals_and_falls_back_to_the_count():
    snap = _snap(
        steps=[_step(STEP1, 0, tools=["traces_search"], results=1), _parallel_step([1])]
    )
    # Ordinal 1 without 0 is not a prefix: only the actual ordinal counts.
    assert h.committed_ops(snap) == {f"{STEP1}-t0", f"{STEP2}-t1"}


def _stall_snap(ordinals, sibling_seconds=1.0, epoch=1):
    return _snap(
        epoch=epoch,
        steps=[
            _step(STEP1, 0, tools=["traces_search"], results=1),
            _parallel_step(ordinals),
        ],
        reservations=[_res("r1"), _res("r2")],
        charges=[
            _charge(f"{STEP1}-t0", epoch),
            _charge(f"{STEP2}-t0", epoch, seconds=sibling_seconds, dispatch="sibling"),
            _charge(f"{STEP2}-t1", epoch, seconds=None, dispatch="stalled"),
        ],
        evidence=[EV1],
    )


def test_tool_breakpoint_waits_for_a_settled_sibling_to_commit():
    # The sibling (ordinal 0) settled; its result is not in tool_results yet.
    assert h.tool_breakpoint_state(_stall_snap([])) is None
    assert h.settled_uncommitted(_stall_snap([]), 1, STEP2) == [f"{STEP2}-t0"]
    assert h.tool_breakpoint_state(_stall_snap([0])) is not None
    # A sibling still in flight: the stall is not the only unsettled dispatch.
    assert h.tool_breakpoint_state(_stall_snap([], sibling_seconds=None)) is None


def test_tool_breakpoint_state_changes_when_a_sibling_lands():
    before = _stall_snap([0])
    after = copy.deepcopy(before)
    after["charges"].append(_charge(f"{STEP2}-tX", seconds=1.0, dispatch="late"))
    assert h.tool_breakpoint_state(before) != h.tool_breakpoint_state(after)
    assert h.tool_breakpoint_state(before) == h.tool_breakpoint_state(
        copy.deepcopy(before)
    )


def test_dwell_needs_the_same_state_for_the_whole_interval():
    candidate, ready = h.dwell(None, "a", 10.0, 1.0)
    assert candidate == ("a", 10.0) and not ready
    candidate, ready = h.dwell(candidate, "a", 10.5, 1.0)
    assert not ready
    candidate, ready = h.dwell(candidate, "b", 10.9, 1.0)
    assert candidate == ("b", 10.9) and not ready
    candidate, ready = h.dwell(candidate, "b", 11.9, 1.0)
    assert ready
    assert h.dwell(candidate, None, 12.0, 1.0) == (None, False)


def test_a_sibling_committed_out_of_order_is_not_redispatched():
    # After #332: ordinal 0 settled and committed while ordinal 1 is stalled;
    # the next epoch dispatches only ordinal 1 again.
    pre = _stall_snap([0])
    post = copy.deepcopy(pre)
    final = _snap(
        epoch=2,
        state="completed",
        steps=[pre["steps"][0], _parallel_step([0, 1])],
        reservations=[_res("r1"), _res("r2")],
        charges=[*pre["charges"], _charge(f"{STEP2}-t1", 2)],
        evidence=[EV1],
        cited=[EV1["evidence_id"]],
    )
    result = h.verdicts(
        "tool_in_flight",
        pre=pre,
        post=post,
        final=final,
        epoch_started_at="2026-10-22T20:07:00+00:00",
        poll_seconds=2.0,
    )
    tool = result["criteria"]["tool_redispatch"]
    assert tool["redispatched"] == [f"{STEP2}-t1"]
    assert tool["settled_uncommitted_at_kill"] == []
    assert tool["passed"]
    assert result["criteria"]["breakpoint_held"]["passed"]


def test_a_committed_higher_ordinal_is_not_reported_as_uncommitted():
    # Ordinal 1 committed, ordinal 0 stalled: a prefix reading of the count
    # would call t1 uncommitted and t0 committed.
    snap = _snap(
        steps=[_parallel_step([1])],
        charges=[
            _charge(f"{STEP2}-t0", seconds=None, dispatch="stalled"),
            _charge(f"{STEP2}-t1", dispatch="sibling"),
        ],
    )
    assert h.settled_uncommitted(snap, 1) == []


def _single_call_stall_snap():
    return _snap(
        steps=[
            _step(STEP1, 0, tools=["traces_search"], results=1),
            _step(
                STEP2,
                1,
                tools=["metrics_range_query"],
                ordinals=[],
                status="response_committed",
            ),
        ],
        charges=[
            _charge(f"{STEP1}-t0"),
            _charge(f"{STEP2}-t0", seconds=None, dispatch="stalled"),
        ],
        evidence=[EV1],
    )


def test_a_stall_in_a_single_call_step_never_forms_breakpoint_three():
    # Live 2026-10-23 (invalid round): the stalled step planned one call.
    snap = _single_call_stall_snap()
    assert h.stalled_dispatch(snap) is not None
    assert h.tool_breakpoint_state(snap) is None
    assert h.stall_without_sibling(snap)["dispatch_id"] == "stalled"


def test_breakpoint_three_needs_at_least_one_committed_sibling():
    snap = _stall_snap([0])
    assert h.committed_siblings(snap) == [f"{STEP2}-t0"]
    assert h.stall_without_sibling(snap) is None
    # Two planned calls but the sibling has no charge (never dispatched):
    # no sibling settled and committed, so no kill.
    no_sibling = copy.deepcopy(snap)
    no_sibling["charges"] = [
        c for c in no_sibling["charges"] if c["dispatch_id"] != "sibling"
    ]
    no_sibling["steps"][1] = _parallel_step([])
    assert h.committed_siblings(no_sibling) == []
    assert h.tool_breakpoint_state(no_sibling) is None
