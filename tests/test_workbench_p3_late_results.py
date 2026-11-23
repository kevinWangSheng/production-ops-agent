"""Workbench P3-1/5/10: a late result (a reply fenced off by a pause,
takeover or lost lease, kept as history) is never an answer, never the step
that read a message, and takes no number in the incident's step count."""

from __future__ import annotations

import re
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


# --- #704: Recovery watch records list every reading's provenance ---------

_SHA = {name: (name[0] * 64) for name in ("alpha", "bravo", "charlie", "delta")}


def _reading(name, *, verified, stored, replayed, verdict="healthy", reason=None):
    return {
        "signal_name": f"sig_{name}",
        "query": f"rate(q_{name}[5m])",
        "source": f"prometheus-{name}",
        "window_start": "2026-11-18T10:00:00+00:00",
        "window_end": "2026-11-18T10:05:00+00:00",
        "raw_sha256": _SHA[name],
        "raw_verified": verified,
        "stored": stored,
        "replayed": replayed,
        "verdict": verdict,
        "reason": reason,
    }


def _with_recovery(snapshot):
    readings = [
        # verified, replay matches
        _reading(
            "alpha", verified=True, stored=("ok", 0.5, 30), replayed=("ok", 0.5, 30)
        ),
        # bundle present but hashes differently: nothing rebuilt
        _reading("bravo", verified=False, stored=("ok", 1.25, 12), replayed=None),
        # no bundle (raw data cleaned): grey, not an inconsistency
        _reading("charlie", verified=None, stored=("ok", None, None), replayed=None),
        # verified bundle, rebuilt reading differs from the stored one
        _reading(
            "delta",
            verified=True,
            stored=("ok", 0.01, 40),
            replayed=("ok", 0.9, 40),
            verdict="degraded",
            reason="ERROR_RATE_ABOVE_THRESHOLD",
        ),
    ]
    sample = {
        "sample_id": "00000000-0000-0000-0000-0000000000a1",
        "sequence": 1,
        "window_start": "2026-11-18T10:00:00+00:00",
        "window_end": "2026-11-18T10:05:00+00:00",
        "submitted_at": "2026-11-18T10:05:01+00:00",
        "stored_outcome": "healthy",
        "stored_required_signals_present": True,
        "replayed_outcome": "degraded",
        "replayed_required_signals_present": True,
        "replayed_reason": None,
        "recompute_skipped": None,
        "disposition": "adopted",
        "reason": "SAMPLE_ADOPTED",
        "confirms_health": True,
        "health_basis": None,
        "transition": None,
        "replayed_decision": None,
        "integrity": ["RAW_HASH_MISMATCH:sig_bravo", "RAW_CLEANED:sig_charlie"],
        "consistent": False,
        "raw_cleaned_only": False,
        "readings": readings,
    }
    session_id = "5e551011-0000-0000-0000-000000000000"
    snapshot["observation_sessions"] = [
        {
            "session_id": session_id,
            "authorization_source": "human",
            "state": "ended",
            "ended_reason": "window_elapsed",
            "health_profile_revision": "rev1",
            "target_revision": 3,
            "authorized_at": "2026-11-18T10:00:00+00:00",
            "authorized_by": "oncall",
            "adopted_count": 1,
            "max_samples": 6,
        }
    ]
    snapshot["recovery"] = [
        {
            "session_id": session_id,
            "state": "ended",
            "authorization_source": "human",
            "ended_reason": "window_elapsed",
            "health_profile_revision": "rev1",
            "handled_at": "2026-11-18T10:06:00+00:00",
            "replay": {
                "available": True,
                "consistent": False,
                "integrity": ["STORED_OBSERVATION_INTEGRITY_MISMATCH"],
                "reasons": [],
                "recovery_verdict": "unknown",
                "recovery_confirmed": False,
                "recomputed_verdict": "degraded",
                "latest_sample_verdict": "degraded",
                "healthy_window_seconds": 0,
                "expected_lifecycle": None,
                "recorded_lifecycle": None,
                "stored_session_state": "ended",
                "replayed_session_state": "ended",
            },
            "samples": [sample],
        }
    ]
    return snapshot


def _render_incident_page(snapshot):
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    from opspilot.web import app as web_app

    env = Environment(
        loader=FileSystemLoader(str(web_app._TEMPLATES)),
        autoescape=select_autoescape(default=True, default_for_string=True),
    )
    actions = ["message"]
    return env.get_template("incident.html").render(
        feedback=None,
        principal=SimpleNamespace(actor_id="oncall", role="operator"),
        snapshot=snapshot,
        release_links=[],
        postmortem=None,
        postmortem_kinds=web_app.EVENT_KINDS,
        live_kinds=web_app.LIVE_KINDS,
        actions=actions,
        view=page_view(snapshot, None, actions, message_available=True),
        form_nonce="n",
    )


def _reading_rows(html):
    """Visible text of each table row in Recovery watch records naming one
    reading, keyed by that reading's signal name."""
    section = html[html.index('id="recwatch"') :]
    section = section[: section.index("</details>")]
    rows = {}
    for chunk in re.split(r"<tr[\s>]", section)[1:]:
        chunk = chunk.split("</tr>")[0]
        text = " ".join(re.sub(r"<[^>]+>", " ", chunk).split())
        text = text.replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&")
        named = [n for n in _SHA if f"sig_{n}" in text]
        if len(named) == 1:
            rows.setdefault(named[0], []).append(text)
    return rows


def test_recovery_watch_records_mark_each_reading_on_its_own_row():
    html = _render_incident_page(_with_recovery(_snapshot([_step(5, watermark=1)])))
    rows = _reading_rows(html)
    # Every reading has its own row (one row per reading, not one cell per sample).
    assert sorted(rows) == ["alpha", "bravo", "charlie", "delta"], rows
    assert all(len(v) == 1 for v in rows.values()), rows
    text = {name: v[0] for name, v in rows.items()}
    for name in _SHA:
        # provenance: query, source and the raw sha256 are visible per reading
        assert f"rate(q_{name}[5m])" in text[name], text[name]
        assert f"prometheus-{name}" in text[name], text[name]
        assert _SHA[name] in text[name], text[name]
    # integrity markers: icon + words, on exactly the right readings
    flagged = {
        name: ("✗ hash mismatch" in t, "✗ replay differs" in t)
        for name, t in text.items()
    }
    assert flagged == {
        "alpha": (False, False),
        "bravo": (True, False),
        "charlie": (False, False),
        "delta": (False, True),
    }, text
    assert "verified" in text["alpha"]
    assert "no bundle" in text["charlie"] and "✗" not in text["charlie"]
    # stored -> replayed comparison with None written as "–", verdict and reason
    assert "0.01" in text["delta"] and "0.9" in text["delta"]
    assert "ERROR_RATE_ABOVE_THRESHOLD" in text["delta"]
    assert "–" in text["charlie"]
