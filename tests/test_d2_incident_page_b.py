"""Independent D2 contract tests: UX spec §§4.1, 4.3–4.8 and §7.

Only public Workbench/view/HTTP interfaces are exercised. No product source
is inspected. The shared memory store predates concluded-Run renewal and
returns no history: the small fixture below supplies those persistence facts.
It does not replace the service's controls/question-actor projection, view
functions, action selection, or HTML renderer. Browser-only behavior is
checked separately; DOM assertions here do not claim browser execution.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import timedelta
from html.parser import HTMLParser
from uuid import UUID

import pytest

from opspilot.web.incident_view import page_view, run_numbers
from opspilot.web.service import available_actions
from tests.m1_incident_control_support import (
    element_text,
    incident_badges,
    summary_words,
)
from tests.m1_investigation_support import reply, report_json, tool_call
from tests.m1_web_support import (
    MemoryIncidentStore,
    ScriptedInvestigator,
    basic,
    build_workbench,
    call,
    post_form,
    same_origin,
    submit_incident,
)

HEADER_ORDER = ("resume", "pause", "cancel", "new_run", "takeover", "reopen", "close")
CONFIRM = {"cancel", "close", "takeover", "merge_into", "rebind_target"}
SUMMARY = "Checkout errors increased in the authorized window."


@dataclass
class Element:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    parent: Element | None = field(default=None, repr=False)

    @property
    def text(self):
        return "".join(c.text if isinstance(c, Element) else c for c in self.children)

    @property
    def words(self):
        return " ".join(self.text.split())

    def find(self, tag=None, **attrs):
        result = []
        for child in self.children:
            if isinstance(child, Element):
                if (tag is None or child.tag == tag) and all(
                    child.attrs.get(k) == v for k, v in attrs.items()
                ):
                    result.append(child)
                result.extend(child.find(tag, **attrs))
        return result

    def one(self, tag=None, **attrs):
        result = self.find(tag, **attrs)
        assert len(result) == 1, (tag, attrs, len(result))
        return result[0]

    def inside(self, ancestor):
        node = self.parent
        while node is not None:
            if node is ancestor:
                return True
            node = node.parent
        return False


class HTML(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.root = Element("document")
        self.stack = [self.root]
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Element(tag, dict(attrs), parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, text):
        self.stack[-1].children.append(text)


class D2MemoryStore(MemoryIncidentStore):
    """Add only the already-contracted renewal persistence to the old double."""

    def control(
        self, incident_id, expected_generation, action, actor, payload=None, **renewal
    ):
        row = self.incidents[incident_id]
        old = self.runs[row["current_run_id"]]
        if (
            row["conclusion"] is not None
            and row["mode"] == "automatic"
            and action in {"follow_up", "correct"}
        ):
            assert expected_generation == row["control_generation"]
            run_id = renewal["renew_run_id"]
            assert run_id is not None and run_id not in self.runs
            generation = expected_generation + 1
            self.runs[run_id] = self._run(
                run_id,
                incident_id,
                generation,
                renewal["renew_deadline"],
                old["budget_limit"],
                old["versions"],
                renewal["renew_input"],
            )
            row.update(
                current_run_id=run_id,
                conclusion=None,
                state="running",
                lifecycle="open",
                control_generation=generation,
            )
            self.controls.append(
                {
                    "incident_id": incident_id,
                    "action": action,
                    "expected": expected_generation,
                    "resulting": generation,
                    "actor": actor,
                    "payload": payload,
                }
            )
            self.inputs.append(
                {
                    "incident_id": incident_id,
                    "sequence": len(self.inputs) + 1,
                    "kind": action,
                    "content": dict(payload or {}),
                    "actor": actor,
                    "control_generation": generation,
                }
            )
            return generation
        return super().control(
            incident_id, expected_generation, action, actor, payload, **renewal
        )


class Case:
    def __init__(self, *, question="Why is checkout erroring?"):
        self.app, self.wb, self.clock = build_workbench()
        store = D2MemoryStore(self.clock)
        store.ledger = self.wb.ledger
        self.wb.incidents = store
        response = submit_incident(self.app, question=question)
        assert response.status == 201
        self.id = response.json()["incident_id"]
        self.subject = UUID(self.id)
        self.records = []
        self.extra = {}
        snapshot = self.wb.snapshot

        def fixture_snapshot(subject):
            data = snapshot(subject)
            current = data["run"]["run_id"]
            # MemoryIncidentStore.investigation_history returns [] and its
            # control_times returns []; supply persisted read-side fixtures.
            data["history"] = [
                copy.deepcopy(r) for r in self.records if r["run_id"] != current
            ]
            for rows in (data["controls"], data["decision_log"]):
                for row in rows:
                    row["decided_at"] = row["decided_at"] or self.clock.now()
            data.update(copy.deepcopy(self.extra))
            return data

        self.wb.snapshot = fixture_snapshot

    @property
    def incident(self):
        return self.wb.incidents.incidents[self.subject]

    def snapshot(self):
        return self.wb.snapshot(self.subject)

    def html(self):
        response = call(self.app, "GET", f"/incidents/{self.id}", headers=basic())
        assert response.status == 200
        return response.text

    def act(self, action, **fields):
        generation = self.incident["control_generation"]
        response = post_form(
            self.app,
            f"/incidents/{self.id}/control",
            {
                "action": action,
                "expected_generation": str(generation),
                "idempotency_key": f"d2-{action}-{generation}",
                **fields,
            },
            headers={**basic(), **same_origin()},
        )
        assert response.status == 200, response.text
        return response

    def finish(self, *, facts=1, other_claims=False, summary=SUMMARY, incomplete=False):
        def report(call_):
            evidence = next(
                json.loads(m["content"])["evidence_id"]
                for m in reversed(call_.messages)
                if m.get("role") == "tool"
            )
            payload = json.loads(
                report_json(
                    evidence_id=evidence,
                    summary=summary,
                    status="incomplete" if incomplete else "completed",
                )
            )
            claim = payload["claims"][0]
            payload["claims"] = [
                dict(claim, text=f"Measured fact {i + 1}.") for i in range(facts)
            ]
            if other_claims:
                payload["claims"].extend(
                    dict(claim, kind=kind, text=text)
                    for kind, text in (
                        ("counter_evidence", "The earlier interval did not rise."),
                        ("hypothesis", "A dependency might be responsible."),
                        ("rejected_hypothesis", "The deploy does not explain timing."),
                        ("recommendation", "Compare the dependency with the SLO."),
                    )
                )
            return reply(content=json.dumps(payload))

        started = self.clock.now()
        outcome = self.wb.run_once(
            self.subject,
            ScriptedInvestigator(
                self.clock,
                replies=[reply(tool_calls=[tool_call()], finish="tool_calls"), report],
            ),
        )
        assert outcome is not None and outcome.execution == "completed"
        assert outcome.handoff is incomplete
        if not incomplete:
            data = self.snapshot()
            self.records.append(
                {
                    "run_id": data["run"]["run_id"],
                    "state": "completed",
                    "control_generation": data["run"]["control_generation"],
                    "started_at": started,
                    "concluded_at": self.clock.now(),
                    "report": copy.deepcopy(data["report"]),
                }
            )
        return outcome


def _more(root):
    matches = [
        d
        for d in root.find("details")
        if any(s.words == "More" for s in d.children if isinstance(s, Element))
    ]
    assert len(matches) == 1
    return matches[0]


def _section(html, title):
    """Use the same first-literal/next-h2 boundary as the F12 contract."""
    start = html.lower().index(title.lower())
    end = html.find("<h2", start)
    return html[start : end if end >= 0 else len(html)]


@pytest.mark.parametrize(
    "phase",
    [
        "running",
        "paused",
        "run_paused",
        "concluded",
        "resolved",
        "human_owned",
        "closed",
        "cancelled",
    ],
)
def test_header_actions_more_forms_and_stored_values(phase):
    c = Case()
    if phase in {"concluded", "resolved", "closed"}:
        c.finish()
    if phase == "running":
        c.wb.incidents.runs[c.incident["current_run_id"]]["state"] = "running"
    elif phase == "paused":
        c.act("pause")
    elif phase == "run_paused":
        c.wb.incidents.runs[c.incident["current_run_id"]]["state"] = "paused"
    elif phase == "human_owned":
        c.act("takeover")
    elif phase == "cancelled":
        c.act("cancel")
    elif phase in {"resolved", "closed"}:
        c.incident["lifecycle"] = phase

    data = c.snapshot()
    html = c.html()
    root = HTML(html).root
    actions = set(available_actions(data))
    fields = root.find("input", name="action")
    assert {f.attrs["value"] for f in fields} == actions
    assert len(fields) == len(actions) == len(root.find("form"))
    assert {
        f.attrs["value"] for f in root.find("input", name="expected_generation")
    } == {str(data["incident"].control_generation)}
    assert re.search(r'name="expected_generation" value="(\d+)"', html)[1] == str(
        data["incident"].control_generation
    )
    more = _more(root)
    paused = data["incident"].state == "paused" or data["run"]["state"] == "paused"
    candidates = [
        a
        for a in HEADER_ORDER
        if a in actions
        and (a != "resume" or paused)
        and (a != "takeover" or data["incident"].mode != "human_owned")
    ]
    main = []
    more_actions = []
    aside = root.one("aside")
    assert aside.one("h2").words == "What happened"
    for form in root.find("form"):
        action = form.one("input", name="action").attrs["value"]
        assert form.attrs["id"] == f"act-{action}"
        assert form.attrs["method"].lower() == "post"
        assert form.attrs["action"] == f"/incidents/{c.id}/control"
        assert ("data-confirm" in form.attrs) is (action in CONFIRM)
        if action in {"follow_up", "correct"}:
            assert form.inside(aside)
        elif form.inside(more):
            more_actions.append(action)
        else:
            main.append(action)
    assert main == candidates[:4]
    assert len(main) <= 4
    assert set(more_actions) == actions - set(main) - {"follow_up", "correct"}
    for action, field_name in (
        ("register_remediation", "revision"),
        ("rebind_target", "to_target"),
        ("merge_into", "into_incident_id"),
    ):
        if action in actions:
            assert root.one("form", id=f"act-{action}").find(name=field_name)

    # §4.8 literals: these assertions must never be loosened to match a redesign.
    assert not {"state", "queued", "running"} & summary_words(html)
    assert not {"queued", "running"} & set(incident_badges(html))
    assert f'id="run-state">{data["run"]["state"]}<' in html
    assert f"<code>{data['run']['run_id']}</code>" in html
    stored = [
        d
        for d in root.find("details")
        if any(s.words == "Stored values" for s in d.children if isinstance(s, Element))
    ]
    assert len(stored) == 1 and "open" not in stored[0].attrs
    for key, expected in (
        ("incident-lifecycle", data["incident"].lifecycle),
        ("incident-mode", data["incident"].mode),
        ("control-generation", str(data["incident"].control_generation)),
        ("run-state", data["run"]["state"]),
        ("concluded", "concluded" if data["incident"].concluded else "no conclusion"),
    ):
        assert stored[0].one(id=key).words == expected
    if phase == "paused":
        assert stored[0].one(id="incident-control").words == "paused"
    elif phase == "cancelled":
        assert stored[0].one(id="incident-control").words == "cancelled"
    status = next(
        p for p in root.find("p") if "stline" in p.attrs.get("class", "").split()
    )
    assert " · Run 1" in status.words
    assert status.words.startswith(
        page_view(data, None, sorted(actions))["status"]["words"]
    )
    if phase == "human_owned":
        assert "by alice" in status.words


def test_ui_header_uses_question_actor_and_current_target():
    question = 'Checkout <script>alert("x")</script> is slow?'
    c = Case(question=question)
    html = c.html()
    root = HTML(html).root
    assert root.one("h1").words == question
    assert "clamp2" in root.one("h1").attrs.get("class", "").split()
    target = root.one(id="current-target")
    assert target.words == "checkout-prod"
    assert "Asked by alice" in target.parent.words
    assert "opened" in target.parent.words
    assert target.parent.find("time")[0].attrs["datetime"]
    assert '<script>alert("x")</script>' not in html
    assert c.snapshot()["question_actor"] == "alice"


@pytest.mark.parametrize(
    "phase,primary",
    [
        ("paused", "resume"),
        ("closed", "reopen"),
        ("resolved", "close"),
    ],
)
def test_primary_header_action_for_paused_closed_and_resolved(phase, primary):
    c = Case()
    if phase == "paused":
        c.act("pause")
    else:
        c.finish()
        c.incident["lifecycle"] = phase
    data = c.snapshot()
    view = page_view(data, None, available_actions(data))
    assert {a["action"] for a in view["actions"] if a["primary"]} == {primary}
    form = HTML(c.html()).root.one("form", id=f"act-{primary}")
    assert (
        form.one("button").words
        == {"resume": "Resume", "reopen": "Reopen", "close": "Close"}[primary]
    )


def test_retarget_and_merge_extra_inputs_are_in_more_with_confirmation_hooks():
    c = Case()
    c.extra["current_target"] = "checkout-prod"
    c.extra["rebind_targets"] = [
        {"target_id": "replacement", "resource_uid": "checkout-v2"}
    ]
    data = c.snapshot()
    assert "rebind_target" in available_actions(data)
    root = HTML(c.html()).root
    more = _more(root)
    for action, name in (
        ("rebind_target", "to_target"),
        ("merge_into", "into_incident_id"),
    ):
        form = root.one("form", id=f"act-{action}")
        assert form.inside(more)
        assert form.find(name=name)
        assert form.attrs["data-confirm"] == action


@pytest.mark.parametrize("count", [0, 3])
def test_status_line_recovery_count_since_and_no_duplicated_watching(count):
    from tests.test_m1_02_recovery_page import _observing_incident

    app, wb, incident, _ = _observing_incident(count=count)
    html = call(app, "GET", f"/incidents/{incident}", headers=basic()).text
    status = next(
        p
        for p in HTML(html).root.find("p")
        if "stline" in p.attrs.get("class", "").split()
    )
    data = wb.snapshot(UUID(incident))
    assert " · Run 1" in status.words
    assert status.words.count("Watching recovery") == 1
    maximum = data["observation_sessions"][0]["max_samples"]
    if count:
        assert f"{count} of {maximum} samples healthy" in status.words
        assert re.search(r"since \d{2}:\d{2}", status.words)
    else:
        assert f"no samples yet (up to {maximum})" in status.words


def test_confirm_sentences_are_not_visible_in_server_rendered_forms():
    c = Case()
    root = HTML(c.html()).root

    def visible_text(node):
        if node.tag in {"script", "style"}:
            return ""
        return "".join(
            visible_text(child) if isinstance(child, Element) else child
            for child in node.children
        )

    visible = visible_text(root)
    for text in ("Cancel Run 1?", "Close this incident?", "Take over?", "Keep running"):
        assert text not in visible
    for form in root.find("form"):
        assert not form.find("script")


@pytest.mark.parametrize(
    "summary,name,expected",
    [
        (
            "Checkout requests are failing",
            "CheckoutErrorHigh",
            "Checkout requests are failing",
        ),
        ("", "CheckoutErrorHigh", "CheckoutErrorHigh"),
        (None, "CheckoutErrorHigh", "CheckoutErrorHigh"),
    ],
)
def test_alert_header_title_and_alertmanager_line(summary, name, expected):
    c = Case()
    # Public snapshot projection: latest alert carries parsed annotations.
    c.extra["alert"] = {
        "latest": {
            "alertname": name,
            "annotations": {"summary": summary},
            "received_at": c.clock.now().isoformat(),
        },
        "resolved": False,
        "outcome": {"deliveries": []},
    }
    html = c.html()
    root = HTML(html).root
    assert root.one("h1").words == expected
    assert f"Alertmanager · {name}" in root.one(id="current-target").parent.words
    assert "Asked by" not in root.one(id="current-target").parent.words
    view = page_view(c.snapshot(), None, available_actions(c.snapshot()))
    assert view["header"]["title"] == expected


def test_question_actor_survives_intake_event_rolling_out():
    c = Case()
    for _ in range(26):
        c.act("pause")
        c.act("resume")
    data = c.snapshot()
    assert len(data["events"]) == 50
    assert all(e["kind"] != "intake_accepted" for e in data["events"])
    assert data["question_actor"] == "alice"
    assert "Asked by alice" in HTML(c.html()).root.one(id="current-target").parent.words


@pytest.mark.parametrize("action", ["follow_up", "correct"])
def test_renewal_control_names_new_run_and_pause_names_none(action):
    c = Case()
    c.finish()
    old = c.snapshot()["run"]["run_id"]
    c.act(action, text="Check the downstream too.")
    data = c.snapshot()
    assert data["run"]["run_id"] != old
    assert data["controls"][-1]["run_id"] == data["run"]["run_id"]
    assert data["controls"][-1]["action"] == action
    c.act("pause")
    assert c.snapshot()["controls"][-1]["run_id"] is None


def test_previous_conclusion_stays_through_new_run_and_pause_then_is_replaced():
    c = Case()
    c.finish(other_claims=True)
    old = c.snapshot()["run"]["run_id"]
    c.act("follow_up", text="Please test the dependency explanation.")
    for pause in (False, True):
        if pause:
            c.act("pause")
        assert c.snapshot()["report"] is None
        html = c.html()
        assert element_text(html, "investigation-report").startswith(
            "Previous conclusion"
        )
        assert "stays until Run 2 publishes" in html
        assert SUMMARY in html
        history = _section(html, "Investigation history")
        assert old in history and SUMMARY in history
        assert "A dependency might be responsible." in history
        assert "superseded by Run 2" not in history
    c.act("resume")
    c.finish(summary="The downstream explanation has been revised.")
    html = c.html()
    assert element_text(html, "investigation-report") == "Conclusion"
    assert "revises Run 1" in html
    assert "superseded by Run 2" in _section(html, "Investigation history")


def test_run_numbers_sort_started_at_nulls_last_then_generation_then_id():
    c = Case()
    data = c.snapshot()
    current = data["run"]["run_id"]
    t0 = c.clock.now()
    data["steps"] = [{"observed_at": t0, "tools": []}]
    data["history"] = [
        {"run_id": "null-later", "started_at": None, "control_generation": 9},
        {
            "run_id": "same-b",
            "started_at": t0 - timedelta(seconds=1),
            "control_generation": 2,
        },
        {
            "run_id": "older",
            "started_at": t0 - timedelta(seconds=2),
            "control_generation": 8,
        },
        {
            "run_id": "same-a",
            "started_at": t0 - timedelta(seconds=1),
            "control_generation": 2,
        },
        {"run_id": "null-earlier", "started_at": None, "control_generation": 3},
    ]
    numbers = run_numbers(data)
    assert numbers == {
        "older": 1,
        "same-a": 2,
        "same-b": 3,
        current: 4,
        "null-earlier": 5,
        "null-later": 6,
    }
    data["history"].reverse()
    assert run_numbers(data) == numbers


def test_key_evidence_ids_fold_and_other_report_sections():
    c = Case()
    c.finish(facts=29, other_claims=True)
    assert len(c.snapshot()["report"]["facts"]) == 29
    root = HTML(c.html()).root
    key = root.one("ol", id="key-evidence")
    items = [item for item in key.children if isinstance(item, Element)]
    assert len(items) == 30
    for index, item in enumerate(items):
        ev = item.one("details")
        assert "ev" in ev.attrs.get("class", "").split()
        assert ev.attrs["id"] == (f"claim-F{index + 1}" if index < 29 else "claim-C1")
        assert ("rest" in item.attrs.get("class", "").split()) is (index >= 8)
        assert "R1 · metrics.range_query" in ev.one("summary").words
        assert ev.find("a")[0].words == "Open full evidence"
        assert not ev.find("pre")  # the evidence payload is lazy, even without JS
    assert any(b.words == "Show 22 more" for b in root.find("button"))
    assert "Counter-evidence" in root.one(id="claim-C1").words
    for key, expected in (
        ("claim-H1", "A dependency might be responsible."),
        ("claim-X1", "The deploy does not explain timing."),
        ("claim-G1", "No HealthProfile was supplied."),
        ("claim-A1", "Compare the dependency with the SLO."),
    ):
        assert expected in root.one(id=key).words
    headings = " ".join(h.words for h in root.find("h3"))
    assert "Hypotheses Not proven" in headings
    assert "Ruled out" in headings and "Unknown" in headings
    assert "Could not see" in root.one(id="coverage-gaps").words
    assert "Next steps advisory, OpsPilot will not act" in headings


def test_one_megabyte_evidence_is_linked_and_kept_out_of_initial_html():
    c = Case()
    c.finish()
    evidence_id = c.snapshot()["report"]["facts"][0]["evidence_ids"][0]
    record = c.wb.evidence._records[evidence_id]
    payload = "LAZY-EVIDENCE-PAYLOAD-" * 50_000
    raw = json.dumps({"body": payload}).encode()
    view = copy.deepcopy(record.view)
    view["content"] = [{"body": payload}]
    encoded_view = json.dumps(
        view, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    c.wb.evidence._records[evidence_id] = replace(
        record,
        raw=raw,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        view=view,
        view_sha256=hashlib.sha256(encoded_view).hexdigest(),
    )
    path = f"/incidents/{c.id}/evidence/{evidence_id}"
    response = call(c.app, "GET", path, headers=basic())
    assert response.status == 200
    assert response.json()["hashes_verified"] is True
    assert len(response.json()["raw_utf8"].encode()) > 1_000_000
    html = c.html()
    assert "LAZY-EVIDENCE-PAYLOAD-" not in html
    assert len(html.encode()) <= 400_000
    evidence = HTML(html).root.one("details", id="claim-F1")
    assert evidence.one("a").attrs["href"] == path


def scale_case(*, live=False):
    """Public read-side fixture: 80 rounds, 400 queries, 19 earlier reports."""
    c = Case()
    c.finish()
    data = c.snapshot()
    report = copy.deepcopy(data["report"])
    t0 = c.clock.now() - timedelta(hours=1)
    c.records = [
        {
            "run_id": f"00000000-0000-0000-0000-{i:012d}",
            "state": "completed",
            "control_generation": i,
            "started_at": t0 + timedelta(minutes=i),
            "concluded_at": t0 + timedelta(minutes=i, seconds=30),
            "report": dict(report, summary=f"Historical conclusion {i}."),
        }
        for i in range(1, 20)
    ]
    c.extra["steps"] = [
        {
            "step_id": f"step-{r}",
            "sequence": r - 1,
            "logical_key": f"round-{r}",
            "status": "tool_result_committed",
            "control_generation": 0,
            "finish_reason": "tool_calls",
            "planned_tools": 5,
            "history_only": False,
            "observed_at": c.clock.now() + timedelta(seconds=r),
            "tools": [
                {
                    "ordinal": i,
                    "tool": "metrics.range_query",
                    "status": "ok",
                    "reason": None,
                    "adopted": True,
                    "evidence_id": f"scale-{r}-{i}",
                }
                for i in range(5)
            ],
        }
        for r in range(1, 81)
    ]
    if live:
        c.incident["conclusion"] = None
        c.wb.incidents.runs[c.incident["current_run_id"]]["state"] = "running"
    return c


@pytest.mark.parametrize("live", [False, True])
def test_80_round_fold_how_literal_all_queries_and_page_size(live):
    c = scale_case(live=live)
    html = c.html()
    assert len(html.encode()) <= 400_000
    # §4.8 deliberately slices at the first closing details.
    literal = '<details class="how" id="how">'
    assert html.count(literal) == 1
    block = html.split(literal, 1)[1].split("</details>", 1)[0]
    assert "<details" not in block
    how = HTML(html).root.one("details", id="how")
    assert not how.find("details")
    assert "open" not in how.attrs  # live opens this literal via inline script
    rounds = [
        n for n in how.find() if re.fullmatch(r"round-\d+", n.attrs.get("id", ""))
    ]
    assert len(rounds) == 80
    assert [r.attrs["id"] for r in rounds] == [f"round-{i}" for i in range(1, 81)]
    for i, row in enumerate(rounds, 1):
        assert ("rest" in row.attrs.get("class", "").split()) is (4 <= i <= 75)
    assert any(b.words == "Show 72 more rounds" for b in how.find("button"))
    assert block.count("metrics.range_query") == 400
    links = how.find("a")
    for r in range(1, 81):
        for tool in range(5):
            assert (
                sum(
                    a.attrs.get("href")
                    == f"/incidents/{c.id}/evidence/scale-{r}-{tool}"
                    for a in links
                )
                == 1
            )


def test_twenty_runs_history_newest_first_superseded_and_full_report_in_dom():
    c = scale_case()
    html = c.html()
    rows = [
        d
        for d in HTML(html).root.find("details")
        if {"hrun", "investigation-history"} <= set(d.attrs.get("class", "").split())
    ]
    assert len(rows) == 19
    for run_no, row in zip(range(19, 0, -1), rows, strict=True):
        old = c.records[run_no - 1]
        assert row.one("summary").words.startswith(
            f"Run {run_no} · {old['run_id'][:8]} · concluded {old['concluded_at']:%H:%M}"
        )
        assert "superseded by Run 20" in row.words
        assert sum(code.words == old["run_id"] for code in row.find("code")) == 1
        assert old["report"]["summary"] in row.words
        assert old["report"]["facts"][0]["text"] in row.words
        assert not row.find("h2")
        assert "open" not in row.attrs
    assert html.count(SUMMARY) == 1
    assert not HTML(html).root.one(id="raw").find("h2", id="investigation-report")


@pytest.mark.parametrize(
    "state,label",
    [
        ("cancelled", "cancelled"),
        ("waiting_human", "handed off"),
        ("queued", "no conclusion"),
    ],
)
def test_history_summaries_for_runs_without_published_conclusions(state, label):
    c = Case()
    old = "12345678-1234-1234-1234-123456789012"
    c.records = [
        {
            "run_id": old,
            "state": state,
            "started_at": c.clock.now(),
            "control_generation": 0,
            "report": None,
            "concluded_at": None,
        }
    ]
    html = c.html()
    history = _section(html, "Investigation history")
    assert old in history
    assert f"Run 1 · {old[:8]} · {label}" in HTML(history).root.one("summary").words
    assert "superseded by" not in history


def test_human_owned_notes_only_include_text_after_takeover_and_no_new_run():
    c = Case()
    c.act("follow_up", text="Before takeover context.")
    c.act("takeover")
    run_id = c.snapshot()["run"]["run_id"]
    c.act("follow_up", text="Operator checked the pod.")
    c.act("correct", text="The deploy was unrelated.")
    html = c.html()
    headings = [h for h in HTML(html).root.find("h2") if h.words.startswith("Notes")]
    assert len(headings) == 1
    assert re.fullmatch(r"Notes \(?2\)? · recorded, no Run", headings[0].words)
    notes = _section(html, ">Notes")
    assert "Operator checked the pod." in notes and "The deploy was unrelated." in notes
    assert "Before takeover context." not in notes
    assert c.snapshot()["run"]["run_id"] == run_id
    assert len(c.wb.incidents.runs) == 1


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("new", "No conclusion yet."),
        ("cancelled", "Run 1 was cancelled."),
        ("handoff", "Run 1 handed this incident to a human."),
        ("takeover", "Run 1 stopped when alice took over."),
    ],
)
def test_report_missing_texts_and_handoff_stays_unpublished(kind, expected):
    c = Case()
    if kind == "cancelled":
        c.act("cancel")
    elif kind == "takeover":
        c.act("takeover")
    elif kind == "handoff":
        c.finish(incomplete=True)
    html = c.html()
    assert element_text(html, "report-missing").startswith(expected)
    assert element_text(html, "investigation-report") == "Conclusion"
    assert c.snapshot()["report"] is None
    if kind == "handoff":
        assert "Unpublished handoff report, not the conclusion" in html
        assert element_text(html, "handoff-report") is not None
        assert c.snapshot()["handoff_report"]["summary"] not in _section(
            html, "Human control"
        )


def test_incomplete_handoff_has_the_human_readable_incomplete_marker():
    # §4.6 metadata: incomplete is distinct from an inconclusive finding.
    c = Case()
    c.finish(incomplete=True)
    assert c.snapshot()["handoff_report"]["incomplete"] is True
    assert "Incomplete investigation" in c.html()


def test_handoff_preserves_assessment_conclusion_and_hash_under_report_status():
    # §4.6 keeps Assessment, conclusion and sha256 under the existing id.
    c = Case()
    outcome = c.finish(incomplete=True)
    status = element_text(c.html(), "report-status")
    assert status is not None
    assert "incomplete" in status and "inconclusive" in status
    assert outcome.report_content_sha256 in status


def test_human_control_and_decisions_first_occur_in_raw_records_with_rows():
    c = Case()
    c.act("pause")
    c.act("resume")
    html = c.html()
    raw = HTML(html).root.one(id="raw")
    raw_at = html.index('id="raw"')
    for literal in ("Human control", "Human decisions"):
        assert html.lower().index(literal.lower()) > raw_at
        assert literal in raw.text
    rows = _section(html, "Human decisions")
    assert "<tr>" in rows
    for action, generation in (("pause", 1), ("resume", 2)):
        assert f"<code>{action}</code>" in rows
        assert f"→ {generation}<" in rows
    assert "<code>alice</code>" in rows
    assert "01:05" in rows
