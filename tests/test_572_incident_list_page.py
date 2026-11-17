"""Independent contract tests for the redesigned incident list page (#572)."""

from __future__ import annotations

from html.parser import HTMLParser
from uuid import UUID, uuid4

import pytest

from opspilot.web.store import MappingTargetRegistry
from tests.m1_incident_control_support import element_text, list_row
from tests.m1_web_support import (
    ScriptedInvestigator,
    basic,
    build_workbench,
    call,
    post_form,
    same_origin,
    submit_incident,
)


class _ListPage(HTMLParser):
    """Read table rows, form controls, and the header row from public HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[str] = []
        self._tbody = False
        self._thead = False
        self._row: dict | None = None
        self._cell: dict | None = None
        self.rows: list[dict] = []
        self.th_rows: list[dict] = []
        self.forms: list[dict] = []
        self._form: dict | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        self._stack.append(tag)
        if tag == "thead":
            self._thead = True
        elif tag == "tbody":
            self._tbody = True
        elif tag == "tr":
            self._row = {
                "class": set((attributes.get("class") or "").split()),
                "in_thead": self._thead,
                "cells": [],
            }
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = {
                "tag": tag,
                "text": [],
                "links": [],
                "attrs": attributes,
            }
        elif tag == "a" and self._cell is not None:
            href = attributes.get("href")
            if href is not None:
                self._cell["links"].append(href)
        elif tag == "form":
            self._form = {
                "attrs": attributes,
                "controls": [],
            }
        elif (
            tag in {"input", "textarea", "select", "option"} and self._form is not None
        ):
            self._form["controls"].append({"tag": tag, "attrs": attributes, "text": []})

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell["text"].append(data)
        if self._form is not None and self._form["controls"]:
            self._form["controls"][-1]["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            cell = {
                "tag": self._cell["tag"],
                "text": " ".join("".join(self._cell["text"]).split()),
                "links": tuple(self._cell["links"]),
                "attrs": self._cell["attrs"],
            }
            self._row["cells"].append(cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            row = self._row
            if any(cell["tag"] == "th" for cell in row["cells"]):
                self.th_rows.append(row)
            elif any(
                row_link.startswith("/incidents/")
                for cell in row["cells"]
                for row_link in cell["links"]
            ):
                self.rows.append(row)
            self._row = None
        elif tag == "thead":
            self._thead = False
        elif tag == "tbody":
            self._tbody = False
        elif tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None
        if self._stack:
            self._stack.pop()


def _parse(html: str) -> _ListPage:
    page = _ListPage()
    page.feed(html)
    page.close()
    return page


def _index(app, path="/") -> str:
    response = call(app, "GET", path, headers=basic())
    assert response.status == 200
    return response.text


def _assert_row_present(html: str, incident_id: str) -> None:
    page = _parse(html)
    href = f"/incidents/{incident_id}"
    matches = [
        row
        for row in page.rows
        if href in [link for cell in row["cells"] for link in cell["links"]]
    ]
    assert len(matches) == 1


def _detail(app, incident_id: str) -> str:
    response = call(app, "GET", f"/incidents/{incident_id}", headers=basic())
    assert response.status == 200
    return response.text


def _control(app, incident_id: str, action: str, generation: int = 0) -> None:
    response = post_form(
        app,
        f"/incidents/{incident_id}/control",
        {
            "action": action,
            "expected_generation": str(generation),
            "idempotency_key": f"list-{action}-{generation}",
        },
        headers={**basic(), **same_origin()},
    )
    assert response.status == 200, response.text


def _claim(app_workbench, incident_id: str) -> None:
    subject = UUID(incident_id)
    run_id = app_workbench.incidents.incidents[subject]["current_run_id"]
    app_workbench.incidents.claim(subject, run_id, uuid4(), {"state": "v1"}, 600)


@pytest.mark.parametrize("run_state", ["queued", "running"])
def test_active_runs_are_in_the_in_progress_tab_and_keep_the_raw_run_value(run_state):
    """UX §3.3 maps active Runs to In progress and preserves raw Run cells."""
    app, workbench, _ = build_workbench()
    incident_id = submit_incident(app, key=f"run-{run_state}").json()["incident_id"]
    if run_state == "running":
        _claim(workbench, incident_id)

    html = _index(app, "/?tab=in_progress")
    _assert_row_present(html, incident_id)
    assert f'href="/incidents/{incident_id}"' not in _index(app)
    assert list_row(html, incident_id)["Run"] == run_state


@pytest.mark.parametrize("control", ["paused", "cancelled"])
def test_paused_and_cancelled_controls_need_a_human_and_keep_the_raw_control_value(
    control,
):
    """UX §3.3 puts paused/cancelled incidents in Needs a human, preserving Control."""
    app, workbench, _ = build_workbench()
    incident_id = submit_incident(app, key=f"control-{control}").json()["incident_id"]
    if control == "paused":
        _claim(workbench, incident_id)
    _control(app, incident_id, control[:-1] if control == "paused" else "cancel")

    html = _index(app, "/?tab=needs_human")
    _assert_row_present(html, incident_id)
    assert f'href="/incidents/{incident_id}"' not in _index(app, "/?tab=in_progress")
    assert list_row(html, incident_id)["Control"] == control


def test_manual_question_is_visible_in_the_incident_row_without_requiring_full_text():
    """U1 keeps the intake question in the row and permits the long-question display to clip."""
    question = "Why is checkout returning intermittent 503 responses? " + (
        "details " * 40
    )
    app, _, _ = build_workbench()
    incident_id = submit_incident(app, key="long-question", question=question).json()[
        "incident_id"
    ]

    html = _index(app, "/?tab=all")
    row = list_row(html, incident_id)
    assert question[:48] in row["Title"]


def test_concluded_column_reads_no_when_no_report_was_published():
    """U5 keeps the legacy Concluded marker as ``no`` when the incident has no conclusion."""
    app, _, _ = build_workbench()
    incident_id = submit_incident(app, key="no-conclusion").json()["incident_id"]

    assert list_row(_index(app, "/?tab=all"), incident_id)["Concluded"] == "no"


def test_published_report_shows_its_level_and_first_summary_sentence():
    """UX §3.2 moves the conclusion level and summary from the list to detail."""
    app, workbench, clock = build_workbench()
    incident_id = submit_incident(app, key="published").json()["incident_id"]
    outcome = workbench.run_once(
        UUID(incident_id), ScriptedInvestigator(clock, model_requests=4)
    )
    assert outcome is not None and outcome.execution == "completed"

    conclusion = element_text(_detail(app, incident_id), "conclusion")
    assert conclusion is not None
    assert "conclusion supported" in " ".join(conclusion.lower().split())
    assert "Checkout errors increased in the authorized window." in conclusion


def test_unparseable_conclusion_is_explicit_on_the_detail_page():
    """D11 keeps an unparseable stored conclusion explicit instead of guessing a level."""
    app, workbench, _ = build_workbench()
    incident_id = submit_incident(app, key="bad-conclusion").json()["incident_id"]
    workbench.incidents.incidents[UUID(incident_id)]["conclusion"] = {}

    conclusion = " ".join(_detail(app, incident_id).split())
    assert 'id="concluded">concluded' in conclusion
    assert 'id="report-unparseable"' in conclusion
    assert "Published conclusion could not be parsed" in conclusion


@pytest.mark.parametrize("path", ["/", "/?tab=all&page=1"])
def test_each_incident_detail_href_occurs_once_and_two_index_gets_match_exactly(path):
    """U3 keeps one detail link per incident and D13 makes repeated GET / responses byte-identical."""
    app, _, _ = build_workbench()
    incident_ids = [
        submit_incident(app, key=f"href-{index}").json()["incident_id"]
        for index in range(2)
    ]
    for incident_id in incident_ids:
        _control(app, incident_id, "pause")

    first = _index(app, path)
    second = _index(app, path)
    assert first == second
    for incident_id in incident_ids:
        assert first.count(f"/incidents/{incident_id}") == 1


def test_index_form_keeps_the_legacy_post_contract_and_empty_server_idempotency_value():
    """U6 keeps the POST target and three field names while leaving idempotency generation to the browser."""
    app, _, _ = build_workbench()
    page = _parse(_index(app))
    intake_forms = [f for f in page.forms if f["attrs"].get("action") == "/intake/ui"]
    assert len(intake_forms) == 1
    form = intake_forms[0]
    attrs = form["attrs"]
    assert attrs.get("method") == "post"
    assert attrs.get("action") == "/intake/ui"
    controls = [control for control in form["controls"] if control["attrs"].get("name")]
    assert {control["attrs"]["name"] for control in controls} == {
        "target_id",
        "question",
        "idempotency_key",
    }
    key = next(
        control for control in controls if control["attrs"]["name"] == "idempotency_key"
    )
    assert key["attrs"].get("value", "") == ""
    assert "Ask OpsPilot to investigate" in _index(app)


@pytest.mark.parametrize("registry", ["nonempty", "empty"])
def test_target_control_uses_registered_keys_or_text_fallback(registry):
    """U6 uses a registry-key select when configured and a text input when the registry is empty."""
    targets = None
    expected_keys: set[str] = set()
    if registry == "nonempty":
        entry = {
            "integration_id": "test-integration",
            "cluster_uid": "test-cluster",
            "namespace": "otel-demo",
            "workload": "checkout",
            "health_profile_id": "otel-demo-checkout",
        }
        expected_keys = {"checkout-prod", "checkout-canary"}
        targets = MappingTargetRegistry({key: entry for key in expected_keys})
    app, _, _ = build_workbench(targets=targets)
    page = _parse(_index(app))
    controls = [
        control for control in page.forms[0]["controls"] if control["attrs"].get("name")
    ]
    target_controls = [
        control for control in controls if control["attrs"]["name"] == "target_id"
    ]
    assert len(target_controls) == 1
    target = target_controls[0]
    if expected_keys:
        assert target["tag"] == "select"
        options = {
            control["attrs"].get("value")
            for control in page.forms[0]["controls"]
            if control["tag"] == "option"
        }
        assert {value for value in options if value} == expected_keys
    else:
        assert target["tag"] == "input"


def test_table_headers_are_confined_to_the_single_header_row():
    """UX §3.1 preserves exactly one header row inside thead."""
    app, _, _ = build_workbench()
    submit_incident(app, key="header-row")
    page = _parse(_index(app, "/?tab=all"))
    assert len(page.th_rows) == 1
    assert page.th_rows[0]["in_thead"] is True
