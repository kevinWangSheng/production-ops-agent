"""Audit projection checks on a transaction-local dataset; never migrates PG."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row, tuple_row
from psycopg.types.json import Jsonb

from opspilot.acceptance_audit import (
    AUDIT_CATEGORIES,
    AuditEntry,
    AuditTimeline,
    audit_timeline,
)
from opspilot.persistence.base import Connection
from scripts.f7_audit_rebuild import timeline_summary


@pytest.fixture
def conn() -> Iterator[Connection]:
    dsn = os.environ.get("OPSPILOT_TEST_DATABASE_URL") or os.environ.get(
        "OPSPILOT_LAB_DSN"
    )
    if not dsn:
        pytest.skip("set OPSPILOT_TEST_DATABASE_URL or OPSPILOT_LAB_DSN")
    try:
        connection = psycopg.connect(dsn, row_factory=dict_row, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("test PG unavailable")
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def seed(conn: psycopg.Connection[Any]) -> tuple[Any, Any, Any]:
    incident, run, step = uuid4(), uuid4(), uuid4()
    conn.execute(
        """INSERT INTO opspilot_incidents(incident_id,intake_key,state,current_run_id)
           VALUES(%s,%s,'accepted',%s)""",
        (incident, str(incident), run),
    )
    conn.execute(
        """INSERT INTO opspilot_runs(run_id,incident_id,state,control_generation,
            budget_limit,deadline,versions) VALUES(%s,%s,'queued',0,1000,
            clock_timestamp()+interval '1 hour','{}')""",
        (run, incident),
    )
    conn.execute(
        """INSERT INTO opspilot_inputs(input_id,incident_id,sequence,kind,content,
            control_generation,actor) VALUES(%s,%s,1,'follow_up',%s,0,'operator')""",
        (uuid4(), incident, Jsonb({"text": "DO NOT EXPORT secret"})),
    )
    conn.execute(
        """INSERT INTO opspilot_steps(step_id,run_id,logical_key,status,control_generation)
           VALUES(%s,%s,'round1','response_committed',0)""",
        (step, run),
    )
    conn.execute(
        """INSERT INTO opspilot_input_rounds(run_id,logical_key,control_generation,input_watermark)
           VALUES(%s,'round1',0,1)""",
        (run,),
    )
    conn.execute(
        """INSERT INTO opspilot_subject_events(subject_id,sequence,kind,payload)
           VALUES(%s,1,'intake_accepted',%s),(%s,2,'run_completed',%s)""",
        (
            incident,
            Jsonb({"run_id": str(run), "text": "DO NOT EXPORT secret"}),
            incident,
            Jsonb({"run_id": str(run)}),
        ),
    )
    return incident, run, step


def test_unknown_incident(conn: Connection) -> None:
    with pytest.raises(LookupError):
        audit_timeline(conn, uuid4())


def test_pg_projection_readonly_order_missing_and_safe_detail(conn: Connection) -> None:
    incident, run, step = seed(conn)
    dispatch = uuid4()
    operation = f"{step}-t0"
    conn.execute(
        """INSERT INTO opspilot_tool_charges(dispatch_id,run_id,epoch,operation_id)
           VALUES(%s,%s,1,%s)""",
        (dispatch, run, operation),
    )
    conn.execute(
        """INSERT INTO opspilot_evidence(evidence_id,run_id,subject_id,status,adopted,
            raw,raw_sha256,view,view_sha256,projection_revision,observed_at)
           VALUES(%s,%s,%s,'ok',true,%s,%s,%s,%s,'v1','2099-01-01')""",
        (
            f"{operation}:{dispatch}",
            str(run),
            str(incident),
            b"DO NOT EXPORT secret",
            "a" * 64,
            Jsonb({"tool": "prometheus_query", "target_id": "checkout"}),
            "b" * 64,
        ),
    )
    conn.execute(
        """INSERT INTO opspilot_controls(audit_id,incident_id,action,
           expected_generation,resulting_generation,actor,payload)
           VALUES(%s,%s,'correct',0,1,'operator',%s)""",
        (uuid4(), incident, Jsonb({"text": "DO NOT EXPORT secret"})),
    )
    # Rollback-only test data is visible on the same connection. Enforce
    # readonly for the projection using a separate transaction-local role
    # independent check below; no data committed to the shared test DB.
    result = audit_timeline(conn, incident)
    assert result.counts["input"] == 2
    assert result.counts["query"] == 1 and result.counts["evidence"] == 1
    assert result.counts["human_correction"] == 1
    assert "recovery_observation" in result.missing
    assert "postmortem_review" in result.missing
    assert [e.sequence for e in result.entries] == list(range(len(result.entries)))
    assert [e.recorded_at for e in result.entries] == sorted(
        e.recorded_at for e in result.entries
    )
    assert all(e.recorded_at.year != 2099 for e in result.entries)
    query = next(e for e in result.entries if e.category == "query")
    assert query.detail["tool"] == "prometheus_query"
    assert query.detail["outbound"] is True
    assert query.detail["result_code"] == "ok"
    control = next(e for e in result.entries if e.category == "human_correction")
    assert control.actor == "operator" and control.detail["action"] == "correct"
    assert "DO NOT EXPORT" not in repr(result)
    assert timeline_summary(result) == timeline_summary(audit_timeline(conn, incident))


def test_actual_readonly_transaction(conn: Connection) -> None:
    conn.execute("SET TRANSACTION READ ONLY")
    with pytest.raises(LookupError):
        audit_timeline(conn, uuid4())
    assert conn.execute("SHOW transaction_read_only").fetchone() == {
        "transaction_read_only": "on"
    }


def test_frozen_summary_contains_only_aggregates() -> None:
    incident = uuid4()
    entry = AuditEntry(
        datetime(2026, 1, 1, tzinfo=UTC),
        0,
        "input",
        "opspilot_inputs",
        str(uuid4()),
        None,
        "private-actor",
        {"kind": "follow_up"},
    )
    timeline = AuditTimeline(
        incident,
        (entry,),
        {c: int(c == "input") for c in AUDIT_CATEGORIES},
        tuple(c for c in AUDIT_CATEGORIES if c != "input"),
    )
    summary = timeline_summary(timeline)
    assert len(summary["entries_sha256"]) == 64
    assert "private-actor" not in json.dumps(summary)
    assert "follow_up" not in json.dumps(summary)
    with pytest.raises(FrozenInstanceError):
        entry.sequence = 4  # type: ignore[misc]


def test_failure_checkpoint_handoff_and_unanchored_rows(conn: Connection) -> None:
    from opspilot.acceptance_audit import audit_records

    incident, run, step = seed(conn)
    dispatch = uuid4()
    conn.execute(
        """INSERT INTO opspilot_tool_charges(dispatch_id,run_id,epoch,operation_id)
           VALUES(%s,%s,1,%s)""",
        (dispatch, run, f"{step}-t0"),
    )
    conn.execute(
        "UPDATE opspilot_steps SET tool_results=%s WHERE step_id=%s",
        (
            Jsonb(
                [
                    {
                        "ordinal": 0,
                        "result": {
                            "operation_id": f"{step}-t0",
                            "status": "timeout",
                            "reason": "SOURCE_TIMEOUT",
                            "tool": "prometheus_query",
                            "target_id": "checkout",
                            "source_contact": "possible",
                        },
                    }
                ]
            ),
            step,
        ),
    )
    conn.execute(
        """INSERT INTO opspilot_input_rounds(run_id,logical_key,control_generation,input_watermark)
           VALUES(%s,'pending-round',0,1)""",
        (run,),
    )
    conn.execute(
        """INSERT INTO opspilot_subject_events(subject_id,sequence,kind,payload)
           VALUES(%s,3,'run_handoff',%s),(%s,4,'run_handoff',%s)""",
        (
            incident,
            Jsonb({"run_id": str(run), "parked": False}),
            incident,
            Jsonb({"run_id": str(run), "parked": True}),
        ),
    )
    timeline, gaps = audit_records(conn, incident)
    query = next(e for e in timeline.entries if e.category == "query")
    assert query.detail["result_code"] == "timeout"
    assert query.detail["reason"] == "SOURCE_TIMEOUT"
    assert query.detail["tool"] == "prometheus_query"
    assert query.detail["target_id"] == "checkout"
    assert query.detail["outbound"] is None
    assert query.detail["source_contact"] == "possible"
    assert gaps["opspilot_input_rounds"] == 1
    assert [
        e.detail["action"] for e in timeline.entries if e.source == "opspilot_runs"
    ] == ["created", "ended"]
    assert sum(e.source == "opspilot_subject_events" for e in timeline.entries) == 4


def test_default_tuple_connection_keeps_its_row_factory(conn: Connection) -> None:
    with psycopg.connect(conn.info.dsn) as ordinary:
        with ordinary.transaction(force_rollback=True):
            incident, _, _ = seed(ordinary)
            assert ordinary.row_factory is tuple_row
            with ordinary.cursor() as caller_cursor:
                timeline = audit_timeline(ordinary, incident)
                assert timeline.incident_id == incident
                assert timeline.counts["input"] == 2
                assert ordinary.row_factory is tuple_row
                assert caller_cursor.execute("SELECT 1").fetchone() == (1,)
            assert ordinary.execute("SELECT 2").fetchone() == (2,)


@pytest.mark.parametrize(
    "state", ("completed", "cancelled", "failed", "blocked", "budget_exhausted")
)
def test_every_terminal_run_without_an_end_anchor_is_counted_undated(
    conn: Connection,
    state: str,
) -> None:
    from opspilot.acceptance_audit import audit_records

    incident, run, _ = seed(conn)
    conn.execute("UPDATE opspilot_runs SET state=%s WHERE run_id=%s", (state, run))
    conn.execute(
        "DELETE FROM opspilot_subject_events WHERE subject_id=%s AND kind='run_completed'",
        (incident,),
    )
    timeline, gaps = audit_records(conn, incident)
    assert gaps["opspilot_runs.ended"] == 1
    assert not any(
        e.source == "opspilot_runs" and e.detail["action"] == "ended"
        for e in timeline.entries
    )


def test_cancel_through_product_entry_records_run_end(conn: Connection) -> None:
    from opspilot.persistence import DurableStore
    from opspilot.web import (
        Authenticator,
        DurableClock,
        DurableEventLog,
        DurableEvidenceStore,
        DurableIncidentStore,
        DurableWebLedger,
        Workbench,
        create_app,
    )
    from tests.acceptance.f7_pg_support import _auth, scratch_database
    from tests.m1_web_support import (
        UI_USER,
        basic,
        bearer,
        post_form,
        post_json,
        same_origin,
    )

    # The connection fixture checks availability first. Scratch data is dropped
    # after the actual HTTP intake/control paths finish; no model or cluster.
    with contextmanager(scratch_database)() as dsn, closing(DurableStore(dsn)) as store:
        workbench = Workbench(
            incidents=DurableIncidentStore(store),
            events=DurableEventLog(store),
            evidence=DurableEvidenceStore(store),
            ledger=DurableWebLedger(store),
            run_versions={"state": "v1"},
            run_seconds=600,
        )
        app = create_app(workbench, Authenticator(_auth()), DurableClock(store))
        accepted = post_json(  # type: ignore[no-untyped-call]
            app,
            "/intake/events",
            {
                "source": "alertmanager",
                "external_event_id": str(uuid4()),
                "target_id": "checkout-prod",
                "question": "audit cancellation",
            },
            headers=bearer(),
        )
        assert accepted.status == 201, accepted.text
        incident = UUID(accepted.json()["incident_id"])
        with store.transaction(snapshot=True) as connection:
            run = connection.execute(
                "SELECT current_run_id FROM opspilot_incidents WHERE incident_id=%s",
                (incident,),
            ).fetchone()
            assert run is not None
            run_id = run["current_run_id"]
        cancelled = post_form(  # type: ignore[no-untyped-call]
            app,
            f"/incidents/{incident}/control",
            {
                "action": "cancel",
                "expected_generation": 0,
                "idempotency_key": str(uuid4()),
            },
            headers={**basic(), **same_origin()},
        )
        assert cancelled.status == 200, cancelled.text
        with store.transaction(snapshot=True) as connection:
            timeline = audit_timeline(connection, incident)
            control = connection.execute(
                "SELECT audit_id,created_at FROM opspilot_controls WHERE incident_id=%s AND action='cancel'",
                (incident,),
            ).fetchone()
        assert control is not None
        [ended] = [
            e
            for e in timeline.entries
            if e.source == "opspilot_runs"
            and e.run_id == run_id
            and e.detail["action"] == "ended"
        ]
        assert ended.recorded_at == control["created_at"]
        assert ended.detail["control_audit_id"] == str(control["audit_id"])
        assert ended.detail["state"] == "cancelled"
        assert ended.actor == UI_USER


@pytest.mark.parametrize(
    "state,execution,ended_count",
    [
        ("blocked", "blocked", 1),
        ("blocked", "failed", 0),
        ("waiting_human", "blocked", 0),
    ],
)
def test_runner_blocked_handoff_anchors_only_a_blocked_run(
    conn: Connection,
    state: str,
    execution: str,
    ended_count: int,
) -> None:
    from opspilot.acceptance_audit import audit_records

    incident, run, _ = seed(conn)
    conn.execute("UPDATE opspilot_runs SET state=%s WHERE run_id=%s", (state, run))
    conn.execute(
        "DELETE FROM opspilot_subject_events WHERE subject_id=%s AND kind='run_completed'",
        (incident,),
    )
    event = conn.execute(
        """INSERT INTO opspilot_subject_events(subject_id,sequence,kind,payload)
           VALUES(%s,2,'run_handoff',%s) RETURNING recorded_at""",
        (
            incident,
            Jsonb({"run_id": str(run), "execution": execution, "parked": False}),
        ),
    ).fetchone()
    assert event is not None
    timeline, gaps = audit_records(conn, incident)
    ended = [
        e
        for e in timeline.entries
        if e.source == "opspilot_runs"
        and e.run_id == run
        and e.detail["action"] == "ended"
    ]
    assert len(ended) == ended_count
    if ended_count:
        assert ended[0].recorded_at == event["recorded_at"]
        assert ended[0].detail["event_sequence"] == 2
        assert "opspilot_runs.ended" not in gaps
    elif state == "blocked":
        assert gaps["opspilot_runs.ended"] == 1


def test_preexisting_suspension_baselines_explain_a_paused_new_run(
    conn: Connection,
) -> None:
    from opspilot.persistence import DurableStore
    from tests.acceptance.f7_pg_support import scratch_database

    with contextmanager(scratch_database)() as dsn, closing(DurableStore(dsn)) as store:
        target = store.register_target("audit-baseline-target")
        unrelated = store.register_target("unrelated-audit-baseline-target")
        global_generation = store.set_global_suspension(
            True, expected_generation=0, actor="operator"
        )
        store.set_global_suspension(
            True, expected_generation=global_generation, actor="retry"
        )
        target_generation = store.set_target_suspension(
            target, True, expected_generation=0, actor="operator"
        )
        target_generation = store.set_target_suspension(
            target, False, expected_generation=target_generation, actor="operator"
        )
        store.set_target_suspension(
            unrelated, True, expected_generation=0, actor="operator"
        )
        with store.transaction(snapshot=True) as connection:
            expected = connection.execute(
                """SELECT DISTINCT ON (target_id) audit_id,target_id,suspended,created_at
                   FROM opspilot_suspension_audit WHERE target_id IS NULL OR target_id=%s
                   ORDER BY target_id,created_at DESC,audit_id DESC""",
                (target,),
            ).fetchall()
            now = connection.execute("SELECT clock_timestamp() AS now").fetchone()
            assert now is not None
        incident, run = uuid4(), uuid4()
        store.accept(
            incident,
            run,
            str(incident),
            deadline=now["now"] + timedelta(hours=1),
            budget_limit=1000,
            versions={"state": "v1"},
            target_id=target,
        )
        with store.transaction(snapshot=True) as connection:
            state = connection.execute(
                "SELECT state FROM opspilot_runs WHERE run_id=%s", (run,)
            ).fetchone()
        assert state == {"state": "paused"}
        store.set_global_suspension(
            False, expected_generation=global_generation, actor="operator"
        )
        store.set_target_suspension(
            target, True, expected_generation=target_generation, actor="operator"
        )
        with store.transaction(snapshot=True) as connection:
            timeline = audit_timeline(connection, incident)
            created = connection.execute(
                "SELECT created_at FROM opspilot_incidents WHERE incident_id=%s",
                (incident,),
            ).fetchone()
        assert created is not None
        suspensions = [
            e for e in timeline.entries if e.source == "opspilot_suspension_audit"
        ]
        baseline = [e for e in suspensions if e.detail.get("baseline") is True]
        assert len(baseline) == 2
        assert {e.record_id for e in baseline} == {
            str(row["audit_id"]) for row in expected
        }
        assert {e.detail["target_id"]: e.detail["suspended"] for e in baseline} == {
            None: True,
            str(target): False,
        }
        assert all(e.recorded_at < created["created_at"] for e in baseline)
        changes = [e for e in suspensions if not e.detail.get("baseline")]
        assert len(changes) == 2
        assert all(e.recorded_at >= created["created_at"] for e in changes)
        assert not any(e.detail["target_id"] == str(unrelated) for e in suspensions)
