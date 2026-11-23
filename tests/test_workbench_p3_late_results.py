"""Workbench P3-1/5/10: a late result (a reply fenced off by a pause,
takeover or lost lease, kept as history) is never an answer, never the step
that read a message, and takes no number in the incident's step count.

#652 (B2-U1/U2 = a, B2-R1..R7, B2-R17): the report's structured root cause
on the incident page (one line under the conclusion, ``id="root-cause"``) and
on the postmortem version page (``id="investigation-root-cause"``, read at
render time from the incident's current report via ``Workbench.snapshot``,
never stored in the draft). Rendered with the app's own Jinja environment.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from opspilot.investigation.reports import ReportV2
from opspilot.web import (
    AuthConfig,
    Authenticator,
    MemoryEventLog,
    PostmortemReview,
    create_app,
    hash_password,
)
from opspilot.web.incident_view import LIVE_KINDS, page_view
from opspilot.web.review import EVENT_KINDS
from opspilot.web.service import categorize_report
from tests.m1_web_support import ORIGIN, UI_PASSWORD, UI_USER, basic, call

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


# -- #652: the report's root cause ------------------------------------------

INCIDENT = UUID("00000000-0000-4000-8000-000000000652")
POSTMORTEM = UUID("00000000-0000-4000-8000-000000000653")
RUN = UUID("00000000-0000-4000-8000-000000000654")
EARLIER_RUN = UUID("00000000-0000-4000-8000-000000000655")
AT = datetime(2026, 11, 22, 12, tzinfo=timezone.utc)
#: The F3 live report's root cause (docs/evidence/f3-acceptance-live/
#: prompt-8688f6e52275/run-1/summary.json): three long evidence ids.
EIDS = [
    "e6f4db30-7538-4a22-98d0-97deaab8c296-t2:d8574e8b-e2d0-4992-8fae-30628b29beec",
    "f6610e4d-9566-4c8f-85d5-5adf8f14407e-t0:fe67520f-6458-4ee3-952d-38d88d334020",
    "f6610e4d-9566-4c8f-85d5-5adf8f14407e-t1:36eb1ce5-08a3-4cde-9839-50163ecc4155",
]
PAYMENT = {
    "kind": "Deployment",
    "namespace": "otel-demo",
    "name": "payment",
    "evidence_ids": EIDS,
}
_ENV = Environment(
    loader=FileSystemLoader(str(_TEMPLATES)),
    autoescape=select_autoescape(default=True, default_for_string=True),
)
_VOID = {"br", "img", "input", "hr", "meta", "link", "wbr", "source", "col"}


def _report(conclusion="supported", root_cause=PAYMENT, *, schema="m0-report-v3"):
    body = {
        "schema_version": schema,
        "assessment_status": "completed",
        "conclusion": conclusion,
        "summary": "Charge requests fail after the flag flip. Spans show it.",
        "claims": [
            {
                "kind": "fact",
                "text": "Charge spans error from 01:10.",
                "evidence_ids": [EIDS[0]],
                "target_refs": ["target:checkout"],
                "time_scope_ref": "window:1",
            }
        ],
        "gaps": ["no payment logs"],
    }
    if conclusion == "incomplete":
        body["assessment_status"] = "incomplete"
        body["conclusion"] = "inconclusive"
    if root_cause is not None:
        body["root_cause"] = root_cause
    return ReportV2.model_validate(body)


def _view_of(report: ReportV2) -> dict:
    return dict(categorize_report(report), parsed=True, content_sha256="ab" * 32)


def _incident_snapshot(report: ReportV2) -> dict:
    incident = SimpleNamespace(
        incident_id=INCIDENT,
        concluded=True,
        mode="automatic",
        lifecycle="open",
        state="completed",
        control_generation=0,
        current_run_id=RUN,
        created_at=AT,
        merged_into_incident_id=None,
    )
    step = {
        "step_id": "s1",
        "observed_at": "2026-11-22T12:00:01+00:00",
        # Only the first root-cause id was queried on this page.
        "tools": [{"evidence_id": EIDS[0], "tool": "query_traces", "status": "ok"}],
        "note": None,
        "final_round": False,
        "planned_tools": 1,
        "history_only": False,
        "conclusion": None,
        "input_watermark": 0,
        "replies": [],
    }
    snapshot = {
        "incident": incident,
        "run": {"run_id": str(RUN), "state": "completed"},
        "steps": [step],
        "runs": [
            {
                "run_id": str(RUN),
                "state": "completed",
                "current": True,
                "origin": {"kind": "started"},
                "steps": [step],
                "inline": True,
                "floor": 0,
                "watermark": 0,
            }
        ],
        "messages": [],
        "report": _view_of(report),
    }
    if report.assessment_status == "incomplete":  # A8: shown, not published
        snapshot["handoff_report"] = snapshot.pop("report")
        snapshot["outcome"] = {"reasons": ["INCOMPLETE_INVESTIGATION"]}
    return snapshot


def _incident_html(report: ReportV2) -> str:
    snapshot = _incident_snapshot(report)
    view = page_view(snapshot, None, [], message_available=True)
    return _ENV.get_template("incident.html").render(
        feedback=None,
        principal=SimpleNamespace(actor_id="alice", display="alice"),
        snapshot=snapshot,
        release_links=[],
        postmortem=None,
        postmortem_kinds=EVENT_KINDS,
        live_kinds=LIVE_KINDS,
        actions=[],
        view=view,
        form_nonce="n",
    )


class _Element(HTMLParser):
    """The text and links inside the element with ``id``."""

    def __init__(self, element_id: str) -> None:
        super().__init__()
        self.target = element_id
        self.depth = 0
        self.found = False
        self.text: list[str] = []
        self.links: list[tuple[str, list[str]]] = []
        self.in_link = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.depth == 0 and attrs.get("id") == self.target:
            self.found = True
            self.depth = 1 if tag not in _VOID else 0
            return
        if self.depth and tag not in _VOID:
            self.depth += 1
            if tag == "a":
                self.links.append((attrs.get("href") or "", []))
                self.in_link = True

    def handle_endtag(self, tag):
        if self.depth and tag not in _VOID:
            self.depth -= 1
            if tag == "a":
                self.in_link = False

    def handle_data(self, data):
        if self.depth:
            self.text.append(data)
            if self.in_link:
                self.links[-1][1].append(data)


def _element(html: str, element_id: str) -> _Element | None:
    parser = _Element(element_id)
    parser.feed(html)
    return parser if parser.found else None


def _words(parts) -> str:
    return " ".join("".join(parts).split())


def test_categorize_report_carries_the_root_cause_object():
    """B2-R1: {kind, namespace, name, evidence_ids} or None; an empty
    namespace is None; a v2 report has none."""
    assert categorize_report(_report())["root_cause"] == PAYMENT
    node = {"kind": "Node", "namespace": "", "name": "w1", "evidence_ids": EIDS[:1]}
    assert categorize_report(_report(root_cause=node))["root_cause"] == {
        **node,
        "namespace": None,
    }
    assert categorize_report(_report(root_cause=None))["root_cause"] is None
    v2 = _report(root_cause=None, schema="m0-report-v2")
    assert categorize_report(v2)["root_cause"] is None


def test_supported_report_shows_root_cause_line_with_evidence_links():
    """B2-U1 a, R2, R3: "Root cause", then ``Kind namespace/name`` as written,
    then links that resolve to the report's evidence ids; never "Confirmed"."""
    html = _incident_html(_report())
    line = _element(html, "root-cause")
    assert line is not None, "no root cause line on a supported report"
    words = _words(line.text)
    assert words.startswith("Root cause"), words
    assert "Deployment otel-demo/payment" in words
    # One line under the conclusion summary, above Key evidence.
    assert html.index('class="lede"') < html.index('id="root-cause"')
    assert html.index('id="root-cause"') < html.index('id="h-key"')
    hrefs = [href for href, _ in line.links]
    for eid in EIDS:
        assert f"/incidents/{INCIDENT}/evidence/{eid}" in hrefs, (eid, hrefs)
        # R3: the long id is never the visible link text.
        assert eid not in words
    queried = [t for href, t in line.links if href.endswith(EIDS[0])]
    assert "query_traces · step 1" in _words(queried[0])
    assert "Confirmed" not in html


def _root_line_words(report: ReportV2) -> str:
    line = _element(_incident_html(report), "root-cause")
    assert line is not None, "no root cause line"
    return _words(line.text)


def test_partial_and_cluster_scoped_root_cause_kept_verbatim():
    """partial -> "Likely root cause"; no namespace -> ``Kind name``; kind is
    shown as the report wrote it (no normalisation)."""
    node = {
        "kind": "node",
        "namespace": None,
        "name": "worker-1",
        "evidence_ids": EIDS[1:2],
    }
    words = _root_line_words(_report("partial", node))
    assert words.startswith("Likely root cause"), words
    assert "node worker-1" in words
    assert "/worker-1" not in words
    words = _root_line_words(
        _report("partial", {**node, "kind": "Node", "namespace": ""})
    )
    assert "Node worker-1" in words and "/worker-1" not in words


@pytest.mark.parametrize(
    "report",
    [
        _report("supported", None),
        _report("inconclusive", PAYMENT),
        _report("incomplete", PAYMENT),  # A8: always inconclusive (B2-R4)
        _report("supported", None, schema="m0-report-v2"),
    ],
    ids=["null-root-cause", "inconclusive", "incomplete", "v2-report"],
)
def test_root_cause_line_hidden(report):
    html = _incident_html(report)
    assert 'class="lede"' in html, "the report itself must still render"
    assert _element(html, "root-cause") is None
    assert "otel-demo/payment" not in html
    assert "Likely root cause" not in html


def test_hostile_root_cause_name_is_escaped():
    hostile = {**PAYMENT, "name": '<script>alert("rc")</script>'}
    html = _incident_html(_report(root_cause=hostile))
    assert '<script>alert("rc")</script>' not in html
    assert "&lt;script&gt;" in html
    assert 'otel-demo/<script>alert("rc")</script>' in _root_line_words(
        _report(root_cause=hostile)
    )


# -- postmortem version page (B2-U2 a, B2-R6) --------------------------------


class _Knowledge:
    """The knowledge reads ``PostmortemReview.version_page`` makes: one
    version under review, drafted from ``RUN``."""

    def postmortem(self, postmortem_id):
        row = {
            "version": 1,
            "state": "under_review",
            "stale_reason": None,
            "revises_version": None,
            "created_at": AT,
            "generated_by": "postmortem-worker",
            "generated_by_kind": "worker",
            "conclusions": [],
            "proposals": [],
            "content": "{}",
            "incident_control_generation": 0,
            "observation_generation": 0,
            "observation_session_id": UUID(int=1),
            "observation_ending_id": UUID(int=2),
            "run_count": 1,
            "last_run_id": RUN,
            "input_watermark": 0,
            "evidence_snapshot_sha256": "0" * 64,
        }
        return {
            "incident_id": INCIDENT,
            "postmortem_id": POSTMORTEM,
            "generation": 1,
            "latest_version": 1,
            "versions": [row],
        }

    def audit_trail(self, kind, object_id):
        return []

    def knowledge_from_postmortem(self, postmortem_id):
        return []

    def generation_attempts(self, incident_id):
        return {"attempts": [], "schedule": None}


class _Workbench:
    """The incident reads the version page makes; ``snapshot`` is the
    incident's current state (current Run ``current_run``)."""

    def __init__(self, report, current_run):
        self.report = report
        self.current_run = current_run

    def incident_subject(self, incident_id):
        return {
            "alert_name": "PaymentFailures",
            "service": "otel-demo/checkout",
            "question": None,
            "target": "checkout",
            "lifecycle": "open",
        }

    def feedback_for(self, incident_id, snapshot=None):
        return None

    def snapshot(self, incident_id):
        assert incident_id == INCIDENT
        snapshot = (
            _incident_snapshot(self.report)
            if self.report
            else {
                **_incident_snapshot(_report()),
                "report": None,
            }
        )
        snapshot["incident"].current_run_id = self.current_run
        snapshot["run"]["run_id"] = str(self.current_run)
        snapshot["runs"][0]["run_id"] = str(self.current_run)
        return snapshot


def _version_html(report, current_run=RUN) -> str:
    config = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD, salt=b"fixed-salt-16byt")},
        event_tokens={},
        auth_revision="auth-rev-1",
        allowed_origins=frozenset({ORIGIN}),
    )
    app = create_app(
        _Workbench(report, current_run),
        Authenticator(config),
        SimpleNamespace(now=lambda: AT),
        review=PostmortemReview(knowledge=_Knowledge(), events=MemoryEventLog()),
    )
    response = call(
        app, "GET", f"/postmortems/{POSTMORTEM}/versions/1", headers=basic()
    )
    assert response.status == 200, response.text[:500]
    return response.text


def test_postmortem_page_shows_the_current_report_root_cause_outside_the_draft():
    html = _version_html(_report())
    block = _element(html, "investigation-root-cause")
    assert block is not None, "no investigation root cause on the version page"
    words = _words(block.text)
    assert "Investigation root cause" in words
    assert "from the investigation report · not part of this draft" in words
    assert "Deployment otel-demo/payment" in words
    hrefs = [href for href, _ in block.links]
    for eid in EIDS:
        assert f"/incidents/{INCIDENT}/evidence/{eid}" in hrefs, (eid, hrefs)
    assert "the report has changed since this draft" not in html
    assert "Confirmed" not in html


def test_postmortem_page_says_when_the_report_changed_since_the_draft():
    """The incident's current Run is not the draft watermark's last_run_id."""
    block = _element(
        _version_html(_report(), current_run=EARLIER_RUN), "investigation-root-cause"
    )
    assert block is not None
    assert "the report has changed since this draft" in _words(block.text)


@pytest.mark.parametrize(
    "report",
    [None, _report("supported", None), _report("inconclusive", PAYMENT)],
    ids=["no-report", "null-root-cause", "inconclusive"],
)
def test_postmortem_page_without_a_root_cause_shows_no_block(report):
    html = _version_html(report)
    assert _element(html, "investigation-root-cause") is None
    assert "otel-demo/payment" not in html
