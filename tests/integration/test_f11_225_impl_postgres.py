"""#225 fault injection against actual Observer privileges and deferred guards."""

import copy
import os
import time
from uuid import uuid4, uuid5

import psycopg
import pytest
from psycopg.rows import dict_row

from opspilot import schema
from opspilot.observation.release_commands import (
    audit_release,
    end_release_session,
    release_row,
)
from opspilot.observer.release_replay import replay_release_history
from opspilot.persistence import DurableStore
from scripts.m0.postgres_lab import DSN
from tests.integration.test_f11_pr2a_impl_postgres import (
    authorize,
    claim,
    force_due,
    raw_evidence,
)
from tests.integration.test_f11_pr2a_impl_postgres import (
    harness as f11_harness,
)

harness = f11_harness

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)


def expire(oid):
    # Synthetic clock adjustment is test-only; frozen fields remain protected
    # for all product roles. Keep the deadline/released_at check satisfied.
    with psycopg.connect(DSN) as conn:
        conn.execute("SET LOCAL session_replication_role=replica")
        conn.execute(
            "UPDATE opspilot_release_observations SET "
            "released_at=statement_timestamp()-(deadline_seconds+1)*interval '1 second',"
            "deadline_at=statement_timestamp()-interval '1 second' WHERE observation_id=%s",
            (oid,),
        )
        conn.execute(
            "UPDATE opspilot_observation_sessions SET deadline_at="
            "(SELECT deadline_at FROM opspilot_release_observations WHERE observation_id=%s) "
            "WHERE release_observation_id=%s",
            (oid, oid),
        )


def session_row(conn, sid):
    return conn.execute(
        "SELECT * FROM opspilot_observation_sessions WHERE session_id=%s", (sid,)
    ).fetchone()


def deadline_audit(conn, row, *, sid=None, eid=None):
    audit_release(
        conn,
        row,
        {**row, "status": "unknown"},
        action="deadline_expired",
        actor="observer",
        expected=row["control_generation"],
        key="system:deadline",
        fingerprint="a" * 64,
        outcome="applied",
        reason="deadline_expired",
        session_id=sid,
        ending_id=eid,
        receipt={},
    )


def terminal_update(conn, oid):
    conn.execute(
        "UPDATE opspilot_release_observations SET status='unknown',"
        "ended_reason='deadline_expired',ended_at=clock_timestamp(),"
        "updated_at=clock_timestamp() WHERE observation_id=%s",
        (oid,),
    )


@pytest.mark.parametrize(
    "mode", ["pending", "observing", "paused", "scope_revoked", "binding_stale"]
)
def test_legal_sweep_keeps_authorized_and_audit_only_paths(harness, mode):
    web, observer, oid, _, _ = harness
    sid = None
    if mode != "pending":
        sid = authorize(harness).session_id
    if mode == "paused":
        web.control_release(
            oid,
            action="pause",
            actor="human",
            expected_control_generation=0,
            idempotency_key="pause",
            calibrated_policy_revision=None,
        )
    elif mode in ("scope_revoked", "binding_stale"):
        if mode == "scope_revoked":
            owner = DurableStore(DSN)
            owner.set_target_suspension(
                web.get_release(oid).target_id,
                True,
                expected_generation=0,
                actor="tester",
            )
            owner.close()
        else:
            with psycopg.connect(DSN) as conn:
                conn.execute("SET LOCAL session_replication_role=replica")
                conn.execute(
                    "UPDATE opspilot_observation_sessions SET subject_control_generation=subject_control_generation+1 WHERE session_id=%s",
                    (sid,),
                )
        force_due(sid)
        assert observer._claim_release_due(uuid4(), sid, 120) == []
        with psycopg.connect(DSN) as conn:
            assert conn.execute(
                "SELECT state FROM opspilot_observation_sessions WHERE session_id=%s",
                (sid,),
            ).fetchone() == ("revoked",)
    expire(oid)
    assert oid in observer.sweep_expired_releases(limit=10000)
    assert oid not in observer.sweep_expired_releases(limit=10000)
    assert web.get_release(oid).status == "unknown"
    audit = web.release_audit(oid)[-1]
    if mode == "observing":
        assert audit["session_id"] == sid and audit["ending_id"] is not None
        with psycopg.connect(DSN) as conn:
            assert conn.execute(
                "SELECT state,ended_reason FROM opspilot_observation_sessions WHERE session_id=%s",
                (sid,),
            ).fetchone() == ("expired", "deadline_expired")
    else:
        assert audit["session_id"] is None and audit["ending_id"] is None


@pytest.mark.parametrize(
    "fault",
    [
        "audit_only",
        "session_not_ended",
        "wrong_reason",
        "wrong_reason_null_audit",
        "wrong_generation",
        "wrong_generation_null_audit",
        "old_ending",
    ],
)
def test_erroneous_observer_deadline_transaction_is_42501(harness, fault):
    _, observer, oid, _, _ = harness
    sid = authorize(harness).session_id
    expire(oid)
    if fault in ("wrong_generation", "wrong_generation_null_audit"):
        with psycopg.connect(DSN) as conn:
            conn.execute("SET LOCAL session_replication_role=replica")
            conn.execute(
                "UPDATE opspilot_observation_sessions SET observation_generation=observation_generation+1 WHERE session_id=%s",
                (sid,),
            )
    if fault == "old_ending":
        with psycopg.connect(DSN, row_factory=dict_row) as conn:
            end_release_session(
                conn,
                release_row(conn, oid),
                session_row(conn, sid),
                reason="deadline_expired",
                status_after="unknown",
            )
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as raised:
        with psycopg.connect(observer.dsn, row_factory=dict_row) as conn:
            row = release_row(conn, oid)
            eid = None
            if fault == "session_not_ended":
                eid = uuid5(oid, "session-ending:" + str(sid))
                session = session_row(conn, sid)
                conn.execute(
                    "INSERT INTO opspilot_observation_endings(ending_id,session_id,release_observation_id,ended_reason,transition,subject_state_before,subject_state_after,job_id,job_sequence) VALUES(%s,%s,%s,'deadline_expired','release_unknown','observing','unknown',%s,%s)",
                    (
                        eid,
                        sid,
                        oid,
                        session["active_sample_job_id"],
                        session["active_sample_sequence"],
                    ),
                )
            elif fault in (
                "wrong_reason",
                "wrong_reason_null_audit",
                "wrong_generation",
                "wrong_generation_null_audit",
                "old_ending",
            ):
                eid = end_release_session(
                    conn,
                    row,
                    session_row(conn, sid),
                    reason="authority_revoked"
                    if fault.startswith("wrong_reason")
                    else "deadline_expired",
                    status_after=None
                    if fault.startswith("wrong_reason")
                    else "unknown",
                )
            deadline_audit(
                conn,
                row,
                sid=sid if eid and not fault.endswith("null_audit") else None,
                eid=None if fault.endswith("null_audit") else eid,
            )
            terminal_update(conn, oid)
    assert raised.value.sqlstate == "42501"
    assert harness[0].get_release(oid).status == "observing"
    assert all(a["action"] != "deadline_expired" for a in harness[0].release_audit(oid))


@pytest.mark.parametrize(
    "field",
    [
        "expected_control_generation",
        "control_generation_before",
        "control_generation_after",
        "observation_generation_before",
        "observation_generation_after",
        "paused_before",
        "paused_after",
    ],
)
def test_audit_only_requires_matching_generations_and_pause(harness, field):
    _, observer, oid, _, _ = harness
    expire(oid)
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as raised:
        with psycopg.connect(observer.dsn, row_factory=dict_row) as conn:
            row = release_row(conn, oid)
            # Inject the malformed audit before INSERT; append-only rows are
            # never rewritten by the Observer.
            after = {**row, "status": "unknown"}
            before = dict(row)
            expected = row["control_generation"]
            if field == "expected_control_generation":
                expected += 1
            else:
                side = before if field.endswith("before") else after
                key = field.rsplit("_", 1)[0]
                side[key] = not side[key] if key == "paused" else side[key] + 1
            audit_release(
                conn,
                before,
                after,
                action="deadline_expired",
                actor="observer",
                expected=expected,
                key="system:deadline",
                fingerprint="a" * 64,
                outcome="applied",
                reason="deadline_expired",
                session_id=None,
                ending_id=None,
                receipt={},
            )
            terminal_update(conn, oid)
    assert raised.value.sqlstate == "42501"


@pytest.mark.parametrize("order", ["audit_first", "subject_first", "ending_first"])
def test_deferred_check_accepts_complete_deadline_transaction_in_any_order(
    harness, order
):
    _, observer, oid, _, _ = harness
    sid = authorize(harness).session_id
    expire(oid)
    with psycopg.connect(observer.dsn, row_factory=dict_row) as conn:
        row = release_row(conn, oid)
        if order == "subject_first":
            terminal_update(conn, oid)
        if order == "audit_first":
            deadline_audit(conn, row)
        eid = end_release_session(
            conn,
            row,
            session_row(conn, sid),
            reason="deadline_expired",
            status_after="unknown",
        )
        if order != "audit_first":
            deadline_audit(conn, row, sid=sid, eid=eid)
        if order != "subject_first":
            terminal_update(conn, oid)
    assert harness[0].get_release(oid).status == "unknown"


def test_0016_replaces_only_function_and_downgrade_restores_0015():
    # Dedicated database so migration/downgrade never interrupts other tests.
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    name = "f11_225_" + uuid4().hex[:12]
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    dsn = make_conninfo(DSN, dbname=name)
    try:
        schema.upgrade_to(dsn, "0015_release_observation_sessions")

        def function():
            with psycopg.connect(dsn) as conn:
                return conn.execute(
                    "SELECT pg_get_functiondef('opspilot_release_update_evidence()'::regprocedure)"
                ).fetchone()[0]

        original = function()
        schema.upgrade_head(dsn)
        hardened = function()
        assert hardened != original
        with psycopg.connect(dsn) as conn:
            assert conn.execute(
                "SELECT prosecdef,proconfig FROM pg_proc WHERE oid='opspilot_release_update_evidence()'::regprocedure"
            ).fetchone() == (False, ["search_path=pg_catalog, pg_temp"])
            assert conn.execute(
                "SELECT tgdeferrable,tginitdeferred FROM pg_trigger WHERE tgname='release_update_evidence'"
            ).fetchone() == (True, True)
        schema.command.downgrade(
            schema._config(dsn), "0015_release_observation_sessions"
        )
        assert function() == original
        schema.upgrade_head(dsn)
        assert function() == hardened
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.mark.parametrize(
    ("reason", "harness"),
    [("healthy_confirmed", 40), ("anomaly_detected", 40), ("max_samples_exhausted", 2)],
    indirect=["harness"],
)
def test_sample_endings_require_the_complete_reason_edge(harness, reason):
    _, observer, oid, _, _ = harness
    sid = authorize(harness).session_id
    context = observer.release_context(sid)
    if reason != "max_samples_exhausted":
        time.sleep(2.05)
    lease = claim(harness, sid)
    receipt = observer.submit_release_sample(
        lease,
        raw_evidence(
            context, observer.current_time(), degraded=reason == "anomaly_detected"
        ),
    )
    if reason in ("healthy_confirmed", "max_samples_exhausted"):
        assert receipt.release_status == "observing"
        if reason == "healthy_confirmed":
            time.sleep(1.05)
        lease = claim(harness, sid)
        receipt = observer.submit_release_sample(
            lease, raw_evidence(context, observer.current_time())
        )
    assert receipt.accepted
    history = observer.release_history(oid)
    assert history["endings"][0]["ended_reason"] == reason
    assert replay_release_history(history).consistent
    for field, value in (
        ("subject_state_before", "pending"),
        ("subject_state_after", "cancelled"),
        ("transition", None),
    ):
        corrupted = copy.deepcopy(history)
        corrupted["endings"][0][field] = value
        replay = replay_release_history(corrupted)
        assert not replay.consistent
        assert replay.verdict == "unknown"
        assert "ENDING_MISMATCH" in replay.integrity


@pytest.mark.parametrize("reason", ["cancelled", "deadline_expired"])
def test_paused_pg_history_cannot_forge_a_subject_terminal_ending(harness, reason):
    web, observer, oid, _, _ = harness
    sid = authorize(harness).session_id
    assert (
        web.control_release(
            oid,
            action="pause",
            actor="human",
            expected_control_generation=0,
            idempotency_key="pause-terminal-forgery",
            calibrated_policy_revision=None,
        ).outcome
        == "applied"
    )
    history = observer.release_history(oid)
    assert history["sessions"][0]["session_id"] == sid
    assert replay_release_history(history).consistent
    assert history["release"].status == "observing"
    assert history["endings"][0]["ended_reason"] == "authority_revoked"
    corrupted = copy.deepcopy(history)
    corrupted["sessions"][0].update(
        ended_reason=reason,
        state="expired" if reason == "deadline_expired" else "revoked",
    )
    corrupted["endings"][0].update(
        ended_reason=reason,
        transition="release_unknown" if reason == "deadline_expired" else None,
        subject_state_after="unknown" if reason == "deadline_expired" else "cancelled",
    )
    replay = replay_release_history(corrupted)
    assert not replay.consistent
    assert replay.verdict == "unknown"
    assert "ENDING_MISMATCH" in replay.integrity


@pytest.mark.parametrize(
    "fault",
    [
        "missing_audit",
        "rejected_audit",
        "wrong_audit_session",
        "wrong_audit_generation",
        "wrong_subject_status",
        "wrong_subject_reason",
        "wrong_terminal_time",
        "live_session_without_ending",
    ],
)
def test_cancel_pg_ending_requires_the_actual_terminal_fact(harness, fault):
    from datetime import timedelta

    web, observer, oid, _, _ = harness
    authorize(harness)
    assert (
        web.control_release(
            oid,
            action="cancel",
            actor="human",
            expected_control_generation=0,
            idempotency_key="cancel-terminal-fact",
            calibrated_policy_revision=None,
        ).outcome
        == "applied"
    )
    history = observer.release_history(oid)
    assert replay_release_history(history).consistent
    corrupted = copy.deepcopy(history)
    audit = corrupted["audit"][-1]
    assert audit["action"] == "cancel" and audit["outcome"] == "applied"
    if fault == "missing_audit":
        corrupted["audit"].pop()
    elif fault == "rejected_audit":
        audit["outcome"] = "rejected"
    elif fault == "wrong_audit_session":
        audit["session_id"] = uuid4()
    elif fault == "wrong_audit_generation":
        audit["control_generation_before"] += 1
    elif fault == "live_session_without_ending":
        corrupted["endings"] = []
        corrupted["sessions"][0].update(state="authorized", ended_reason=None)
        audit.update(session_id=None, ending_id=None)
        corrupted["release"] = history["release"].model_copy(
            update={"ended_at": audit["recorded_at"]}
        )
    else:
        updates = {
            "wrong_subject_status": {"status": "observing"},
            "wrong_subject_reason": {"ended_reason": "deadline_expired"},
            "wrong_terminal_time": {
                "ended_at": history["release"].ended_at + timedelta(microseconds=1)
            },
        }
        corrupted["release"] = history["release"].model_copy(update=updates[fault])
    replay = replay_release_history(corrupted)
    assert not replay.consistent
    assert replay.verdict == "unknown"
    assert "ENDING_MISMATCH" in replay.integrity
