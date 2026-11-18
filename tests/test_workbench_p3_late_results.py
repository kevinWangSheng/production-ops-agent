"""Workbench P3-1/5/10: a late result (a reply fenced off by a pause,
takeover or lost lease, kept as history) is never an answer, never the step
that read a message, and takes no number in the incident's step count."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from opspilot.web.incident_view import page_view

T = "2026-11-18T10:00:{:02d}+00:00"


def _step(n, *, watermark, late=False, replies=(), conclusion=None):
    return {
        "step_id": f"s{n}",
        "observed_at": T.format(n),
        "tools": [],
        "note": None,
        "final_round": False,
        "planned_tools": 0,
        "history_only": late,
        "conclusion": conclusion,
        "input_watermark": watermark,
        "replies": [
            {"to_input_seq": seq, "text": "late text", "evidence_ids": []}
            for seq in replies
        ],
    }


def _snapshot(current_steps):
    incident = SimpleNamespace(
        incident_id="i1",
        concluded=False,
        mode="automatic",
        lifecycle="open",
        state="open",
        control_generation=0,
        created_at=datetime(2026, 11, 18, 10, tzinfo=timezone.utc),
        merged_into_incident_id=None,
    )
    handoff = {"execution": "failed", "handoff": True, "reasons": ["CONTROL_DENIED"]}
    earlier = [_step(1, watermark=0), _step(2, watermark=0, conclusion=handoff)]
    return {
        "incident": incident,
        "run": {"run_id": "r2", "state": "waiting_human"},
        "steps": current_steps,
        "runs": [
            {
                "run_id": "r1",
                "state": "cancelled",
                "current": False,
                "origin": {"kind": "started"},
                "steps": earlier,
                "inline": False,
                "floor": 0,
                "watermark": 0,
            },
            {
                "run_id": "r2",
                "state": "waiting_human",
                "current": True,
                "origin": {"kind": "continued"},
                "steps": current_steps,
                "inline": True,
                "floor": 0,
                "watermark": 1,
            },
        ],
        "messages": [
            {
                "sequence": 1,
                "author": "oncall",
                "text": "check the pool",
                "received_at": T.format(3),
                "unread": False,
                "channel": None,
                "retry_of": None,
                "retried": False,
            },
        ],
    }


def test_late_result_is_not_an_answer_a_read_step_or_a_numbered_step():
    current = [
        _step(4, watermark=1, late=True, replies=[1]),
        _step(5, watermark=1),
    ]
    view = page_view(_snapshot(current), None, ["message"], message_available=True)
    entries = view["log"]["entries"]
    (message,) = [e for e in entries if e["kind"] == "human"]
    assert message["status"].startswith("Not answered"), message
    # The first non-late step that covers the message read it: step 2 of the
    # incident's count (step 1 is the earlier Run's), not the late row.
    assert message["read_at_step"] == 2
    assert not [e for e in entries if e["kind"] == "reply"]
    steps = [e for e in entries if e["kind"] == "step"]
    assert [e["number"] for e in steps] == [1, None, 2]
    assert steps[1]["history_only"] is True
    assert view["log"]["steps"] == 2
