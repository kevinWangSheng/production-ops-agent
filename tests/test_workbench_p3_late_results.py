"""Workbench P3-1/5/10: a late result (a reply fenced off by a pause,
takeover or lost lease, kept as history) is never an answer, never the step
that read a message, and takes no number in the incident's step count."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape

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


# -- #744 items 1-3 (B2-R14/R15/R16): the plan card and plan-update steps --

_TEMPLATES = Path(__file__).resolve().parents[1] / "opspilot" / "web" / "templates"
_DOT = r"(?:·|&middot;|&#183;|&#xb7;)"


def _plan_step(n, at, tools):
    step = _step(n, watermark=1)
    step["observed_at"] = at
    step["tools"] = tools
    return step


def _todo(*statuses, prefix):
    return {
        "tool": "TodoWrite",
        "status": "ok",
        "reason": None,
        "task_list": True,
        "todos": [
            {"id": f"{prefix}{i}", "content": f"task {prefix}{i}", "status": s}
            for i, s in enumerate(statuses)
        ],
    }


_QUERY = {"tool": "prometheus_query", "status": "ok", "evidence_id": "ev-1"}


def _log_html():
    # Earlier Run r1 holds step 1 (its handoff row takes no number); this
    # Run's steps are 2, 3 and 4. The plan is written at step 2 (with a
    # query) and last changed at step 3; step 4 only queries.
    current = [
        _plan_step(
            3, "2026-11-18T10:03:30+00:00", [_QUERY, _todo("pending", prefix="a")]
        ),
        _plan_step(
            4, "2026-11-18T10:14:30+00:00", [_todo("completed", "pending", prefix="b")]
        ),
        _plan_step(5, "2026-11-18T10:25:30+00:00", [_QUERY]),
    ]
    snapshot = _snapshot(current)
    view = page_view(snapshot, None, ["message"], message_available=True)
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES)),
        autoescape=select_autoescape(default=True, default_for_string=True),
    )
    page = env.from_string(
        '{% import "_incident_regions.html" as r with context %}{{ r.log_entries() }}'
    )
    return page.render(snapshot=snapshot, view=view)


def test_plan_card_names_the_step_and_time_of_its_latest_update():
    html = _log_html()
    # C1-10 keeps the title; the qualifier says which list this is (B2-R14).
    start = html.index('data-key="plan-r2"')
    end = html.find("data-key=", start + 1)
    card = re.sub(r"<[^>]+>", " ", html[start : end if end > 0 else None])
    assert re.search(
        rf"Investigation plan\s.*?latest\s*{_DOT}\s*updated at step 3, 10:14 UTC",
        card,
        re.S,
    ), card


def test_a_collapsed_step_with_a_query_and_a_plan_update_says_both():
    html = _log_html()
    summaries = re.findall(
        r'<details class="step"[^>]*>\s*<summary>(.*?)</summary>', html, re.S
    )
    assert len(summaries) >= 3, summaries
    text = [re.sub(r"<[^>]+>", " ", s) for s in summaries]
    [both] = [t for t in text if re.search(r"\bStep 2\b", t)]
    assert re.search(rf"\b1 query\s*{_DOT}\s*plan updated\b", both), both
    [query_only] = [t for t in text if re.search(r"\bStep 4\b", t)]
    assert "plan updated" not in query_only, query_only


def test_live_merge_keeps_plan_entries_out_of_the_new_count():
    # Weaker than a browser check (B2-R16): the page's live-merge script must
    # single out ``plan-`` keys so they update in place without counting as
    # new or becoming the chip's scroll target.
    source = (_TEMPLATES / "incident.html").read_text(encoding="utf-8")
    script = source[source.index("<script>") : source.index("</script>")]
    assert re.search(r"""["']plan-["']|/\^plan-/""", script), "no plan- key rule"
