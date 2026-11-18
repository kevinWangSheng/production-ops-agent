"""Workbench P5 F9 (contract r12): feedback names the report it rates; a
report revised since the form was drawn refuses the save (``REPORT_CHANGED``)
and writes nothing, and the page says so and keeps what was written."""

from __future__ import annotations

import re
from urllib.parse import urlencode
from uuid import UUID

from tests.workbench_driver import (  # noqa: F401 -- shared pytest fixtures
    ORIGIN,
    basic,
    call,
    workbench_database_fixture,
    workbench_fixture,
)


def _post(workbench, incident_id, fields, *, html):
    headers = {
        **basic(),
        "origin": ORIGIN,
        "content-type": "application/x-www-form-urlencoded",
    }
    if html:
        headers["accept"] = "text/html"
    return call(
        workbench.app,
        "POST",
        f"/incidents/{incident_id}/feedback",
        headers=headers,
        body=urlencode(fields).encode(),
    )


def test_feedback_for_a_report_no_longer_shown_is_refused(workbench):
    incident_id = UUID(workbench.published("workbench-p5-report-changed").subject_id)
    page = call(workbench.app, "GET", f"/incidents/{incident_id}", headers=basic())
    shown_run = re.search(r'name="rated_run_id" value="([^"]+)"', page.text).group(1)
    stale = {
        "verdict": "incorrect",
        "actual_root_cause": "p5-draft-kept-after-refusal",
        "rated_run_id": shown_run,
        "rated_report_sha256": "0" * 64,
    }
    refused = _post(workbench, incident_id, stale, html=False)
    assert (refused.status, refused.json()["code"]) == (409, "REPORT_CHANGED")
    page = _post(workbench, incident_id, stale, html=True)
    assert page.status == 409
    assert "The report changed while you were writing" in page.text
    assert "p5-draft-kept-after-refusal" in page.text
    assert workbench.feedback_rows(incident_id) == []
