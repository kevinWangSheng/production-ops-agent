"""Deterministic PR-1 implementation checks on the existing PostgreSQL lab.

The follow-up renewal cases of this file were removed with the workbench
redesign P2 (a message is an input, contract B1/O2; the store no longer
knows follow_up/correct). The evidence page check stays until P4's browser
path (a) covers it.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest

from tests.integration.test_m1_incident_control_postgres import _submit
from tests.integration.test_m1_web_postgres import _build, _DbClock
from tests.m1_web_support import (
    ScriptedInvestigator,
    basic,
    call,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)


def _setup(terminal):
    app, workbench, store = _build()
    incident = UUID(_submit(app, f"f12-{uuid4()}"))
    old = store.rebuild(incident)["run"]["run_id"]
    if terminal == "completed":
        result = workbench.run_once(incident, ScriptedInvestigator(_DbClock(store)))
        assert result is not None and result.execution == "completed"
    else:
        store.control(incident, 0, "cancel", "operator")
    return app, workbench, store, incident, old


def test_evidence_html_escapes_raw_text(monkeypatch):
    from dataclasses import replace

    app, workbench, store, incident, _ = _setup("completed")
    report = workbench.snapshot(incident)["report"]
    eid = report["facts"][0]["evidence_ids"][0]
    record = workbench.evidence_for(incident, eid)
    assert record is not None
    monkeypatch.setattr(
        workbench,
        "evidence_for",
        lambda *_: replace(record, raw=b'<script>alert("raw")</script>'),
    )
    response = call(
        app,
        "GET",
        f"/incidents/{incident}/evidence/{eid}",
        headers={**basic(), "accept": "text/html"},
    )
    assert response.status == 200
    assert '<script>alert("raw")</script>' not in response.text
    assert "&lt;script&gt;alert" in response.text
