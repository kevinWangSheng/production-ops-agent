"""#244 PR-2 r3-A: release query results belong to the incidents their
release's anomaly link names; a release no incident consumed keeps its rows."""

from __future__ import annotations

import os

import psycopg
import pytest

from opspilot.persistence.grants import RETENTION_ROLE
from opspilot.retention import run_retention
from scripts.m0.postgres_lab import DSN
from tests.integration.test_f11_pr4a_comparison_impl_postgres import _drain
from tests.integration.test_f11_pr4b_impl_postgres import (  # noqa: F401 - fixtures
    _consume,
    release_harness,
    world,
)
from tests.runtime_roles_support import login

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)


def _release_rows(oid) -> set[str]:
    with psycopg.connect(DSN) as conn:
        return {
            f"{sample}:{key}"
            for sample, key in conn.execute(
                "SELECT q.sample_id, q.query_key FROM opspilot_release_query_results q "
                "JOIN opspilot_observation_samples s USING (sample_id) "
                "WHERE s.subject_id=%s",
                (oid,),
            )
        }


def _objects(report, table: str) -> set[str]:
    return {
        line["object_id"]
        for line in report.lines
        if line.get("table") == table and line["kind"] in ("candidate", "cleaned")
    }


def test_release_rows_follow_the_incident_link(world) -> None:  # noqa: F811
    workbench, _, observer, oid, _ = world
    rows = _release_rows(oid)
    assert rows
    with login(DSN, RETENTION_ROLE) as retention:
        # no incident consumed the anomaly yet: the rows belong to none
        report = run_retention(retention)
        assert not rows & _objects(report, "opspilot_release_query_results")

        _drain(observer)
        receipt = _consume(workbench, oid)
        assert receipt is not None
        incident = receipt.incident_id
        report = run_retention(retention)
        blocked = [
            line
            for line in report.lines
            if line["kind"] == "blocked" and line["incident_id"] == str(incident)
        ]
        assert blocked and "NOT_CLOSED" in blocked[0]["blocked_by"]

        with psycopg.connect(DSN) as conn:
            conn.execute(
                "UPDATE opspilot_runs SET state='cancelled' WHERE incident_id=%s",
                (incident,),
            )
            conn.execute(
                "UPDATE opspilot_incidents SET lifecycle='closed' WHERE incident_id=%s",
                (incident,),
            )
            conn.execute(
                "UPDATE opspilot_incidents SET closed_at=closed_at - interval '31 days' "
                "WHERE incident_id=%s",
                (incident,),
            )
        report = run_retention(retention, apply=True)
        cleaned = {
            line["object_id"]
            for line in report.lines
            if line["kind"] == "cleaned"
            and line["table"] == "opspilot_release_query_results"
            and line["incident_id"] == str(incident)
        }
        assert cleaned == rows
    with psycopg.connect(DSN) as conn:
        assert conn.execute(
            "SELECT count(*) FROM opspilot_release_query_results q "
            "JOIN opspilot_observation_samples s USING (sample_id) "
            "WHERE s.subject_id=%s AND (q.payload_state <> 'cleaned' OR q.raw_sha256 IS NULL)",
            (oid,),
        ).fetchone() == (0,)
