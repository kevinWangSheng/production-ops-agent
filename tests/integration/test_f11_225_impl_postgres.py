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
@pytest.mark.parametrize("active_session", [False, True])
def test_deadline_audit_requires_matching_generations_and_pause(
    harness, field, active_session
):
    _, observer, oid, _, _ = harness
    sid = authorize(harness).session_id if active_session else None
    expire(oid)
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as raised:
        with psycopg.connect(observer.dsn, row_factory=dict_row) as conn:
            row = release_row(conn, oid)
            eid = (
                end_release_session(
                    conn,
                    row,
                    session_row(conn, sid),
                    reason="deadline_expired",
                    status_after="unknown",
                )
                if active_session
                else None
            )
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
                session_id=sid,
                ending_id=eid,
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
            # Audit FKs are immediate: insert the ending first, then the
            # referenced audit while the session is still authorized. Its
            # matching session end is checked at transaction commit.
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
            deadline_audit(conn, row, sid=sid, eid=eid)
            conn.execute(
                "UPDATE opspilot_observation_sessions SET state='expired',ended_reason='deadline_expired',active_sample_job_id=NULL,active_sample_sequence=NULL,active_sample_due_at=NULL,active_sample_owner=NULL,active_sample_lease_until=NULL,updated_at=clock_timestamp() WHERE session_id=%s",
                (sid,),
            )
        else:
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


@pytest.mark.parametrize(
    "fault",
    [
        "missing_audit",
        "null_session",
        "null_ending",
        "wrong_session",
        "wrong_ending",
    ],
)
def test_deadline_session_ending_requires_its_same_transaction_audit(harness, fault):
    web, observer, oid, policy, _ = harness
    sid = authorize(harness).session_id
    old_sid = sid
    if fault.startswith("wrong_"):
        web.control_release(
            oid,
            action="pause",
            actor="human",
            expected_control_generation=0,
            idempotency_key="audit-ref-pause",
            calibrated_policy_revision=None,
        )
        sid = web.control_release(
            oid,
            action="resume",
            actor="human",
            expected_control_generation=1,
            idempotency_key="audit-ref-resume",
            calibrated_policy_revision=policy.revision,
        ).session_id
    expire(oid)
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as raised:
        with psycopg.connect(observer.dsn, row_factory=dict_row) as conn:
            row = release_row(conn, oid)
            eid = end_release_session(
                conn,
                row,
                session_row(conn, sid),
                reason="deadline_expired",
                status_after="unknown",
            )
            if fault != "missing_audit":
                deadline_audit(
                    conn,
                    row,
                    sid=None
                    if fault == "null_session"
                    else old_sid
                    if fault == "wrong_session"
                    else sid,
                    eid=None
                    if fault == "null_ending"
                    else uuid5(oid, "session-ending:" + str(old_sid))
                    if fault == "wrong_ending"
                    else eid,
                )
            terminal_update(conn, oid)
    assert raised.value.sqlstate == "42501"
    assert web.get_release(oid).status == "observing"
    assert all(a["action"] != "deadline_expired" for a in web.release_audit(oid))
    with psycopg.connect(DSN) as conn:
        assert conn.execute(
            "SELECT state,ended_reason FROM opspilot_observation_sessions WHERE session_id=%s",
            (sid,),
        ).fetchone() == ("authorized", None)
        assert conn.execute(
            "SELECT count(*) FROM opspilot_observation_endings WHERE session_id=%s",
            (sid,),
        ).fetchone() == (0,)


@pytest.mark.parametrize("action", ["pause", "resume"])
@pytest.mark.parametrize("reference", ["foreign", "null"])
def test_control_revocation_requires_its_applied_audit_reference(
    harness, action, reference
):
    web, observer, oid, policy, _ = harness
    sid = authorize(harness).session_id
    if action == "resume":
        owner = DurableStore(DSN)
        target = web.get_release(oid).target_id
        generation = owner.set_target_suspension(
            target, True, expected_generation=0, actor="test"
        )
        owner.set_target_suspension(
            target, False, expected_generation=generation, actor="test"
        )
        owner.close()
    command = web.control_release(
        oid,
        action=action,
        actor="human",
        expected_control_generation=0,
        idempotency_key="reference-" + action,
        calibrated_policy_revision=policy.revision if action == "resume" else None,
    )
    assert command.outcome == "applied"
    history = observer.release_history(oid)
    assert replay_release_history(history).consistent
    assert history["endings"][0]["session_id"] == sid
    assert history["endings"][0]["ended_reason"] == "authority_revoked"
    corrupted = copy.deepcopy(history)
    corrupted["audit"][-1]["ending_id"] = uuid4() if reference == "foreign" else None
    replay = replay_release_history(corrupted)
    assert not replay.consistent
    assert replay.verdict == "unknown"
    assert "ENDING_MISMATCH" in replay.integrity


@pytest.mark.parametrize(
    ("reason", "harness"),
    [("healthy_confirmed", 40), ("anomaly_detected", 40), ("max_samples_exhausted", 2)],
    indirect=["harness"],
)
@pytest.mark.parametrize("reference", ["foreign", "null"])
def test_terminal_sample_receipt_requires_its_ending_reference(
    harness, reason, reference
):
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
    corrupted = copy.deepcopy(history)
    terminal = next(
        m
        for m in corrupted["samples"]
        if m["sample_id"] == history["endings"][0]["sample_id"]
    )
    terminal["release_receipt"]["ending_id"] = (
        str(uuid4()) if reference == "foreign" else None
    )
    replay = replay_release_history(corrupted)
    assert not replay.consistent
    assert replay.verdict == "unknown"
    assert "ENDING_MISMATCH" in replay.integrity


def test_control_ending_cannot_be_referenced_by_two_applied_audits(harness):
    from datetime import timedelta

    web, observer, oid, _, _ = harness
    authorize(harness)
    assert (
        web.control_release(
            oid,
            action="pause",
            actor="human",
            expected_control_generation=0,
            idempotency_key="one-ending-reference",
            calibrated_policy_revision=None,
        ).outcome
        == "applied"
    )
    history = observer.release_history(oid)
    assert replay_release_history(history).consistent
    corrupted = copy.deepcopy(history)
    previous = corrupted["audit"][-1]
    duplicate = copy.deepcopy(previous)
    duplicate.update(
        audit_id=uuid4(),
        sequence=previous["sequence"] + 1,
        idempotency_key="duplicate-ending-reference",
        recorded_at=previous["recorded_at"] + timedelta(microseconds=1),
        control_generation_before=previous["control_generation_after"],
        control_generation_after=previous["control_generation_after"] + 1,
        observation_generation_before=previous["observation_generation_after"],
        observation_generation_after=previous["observation_generation_after"] + 1,
        paused_before=True,
    )
    corrupted["audit"].append(duplicate)
    corrupted["release"] = history["release"].model_copy(
        update={
            "control_generation": duplicate["control_generation_after"],
            "observation_generation": duplicate["observation_generation_after"],
        }
    )
    replay = replay_release_history(corrupted)
    assert not replay.consistent
    assert replay.verdict == "unknown"
    assert "ENDING_MISMATCH" in replay.integrity


ENDING_REASONS = (
    "authority_revoked",
    "binding_stale",
    "scope_suspended",
    "cancelled",
    "deadline_expired",
    "healthy_confirmed",
    "anomaly_detected",
    "max_samples_exhausted",
)


def _rename_ending(history, target):
    corrupted = copy.deepcopy(history)
    ending = corrupted["endings"][0]
    sid = ending["session_id"]
    session = next(s for s in corrupted["sessions"] if s["session_id"] == sid)
    session.update(
        ended_reason=target,
        state="completed"
        if target in ("healthy_confirmed", "anomaly_detected")
        else "expired"
        if target in ("deadline_expired", "max_samples_exhausted")
        else "revoked",
    )
    after = (
        "healthy"
        if target == "healthy_confirmed"
        else "anomalous"
        if target == "anomaly_detected"
        else "unknown"
        if target in ("deadline_expired", "max_samples_exhausted")
        else "cancelled"
        if target == "cancelled"
        else ending["subject_state_before"]
    )
    ending.update(
        ended_reason=target,
        subject_state_after=after,
        transition="release_" + after
        if target
        in (
            "healthy_confirmed",
            "anomaly_detected",
            "deadline_expired",
            "max_samples_exhausted",
        )
        else None,
        anomaly_key="release-anomaly:" + str(history["release"].observation_id)
        if target == "anomaly_detected"
        else None,
    )
    return corrupted


@pytest.mark.parametrize(
    ("source", "target", "harness"),
    [
        (s, t, 2 if s == "max_samples_exhausted" else 40)
        for s in ENDING_REASONS
        for t in ENDING_REASONS
        if s != t
    ],
    indirect=["harness"],
)
def test_all_ending_reason_renames_respect_producing_facts_or_r6_limit(
    harness, source, target
):
    from tests.test_f11_225_impl_replay import history_for

    if source in ("healthy_confirmed", "anomaly_detected", "max_samples_exhausted"):
        _, observer, oid, _, _ = harness
        sid = authorize(harness).session_id
        context = observer.release_context(sid)
        if source != "max_samples_exhausted":
            time.sleep(2.05)
        lease = claim(harness, sid)
        receipt = observer.submit_release_sample(
            lease,
            raw_evidence(
                context, observer.current_time(), degraded=source == "anomaly_detected"
            ),
        )
        if source in ("healthy_confirmed", "max_samples_exhausted"):
            assert receipt.release_status == "observing"
            if source == "healthy_confirmed":
                time.sleep(1.05)
            lease = claim(harness, sid)
            receipt = observer.submit_release_sample(
                lease, raw_evidence(context, observer.current_time())
            )
        assert receipt.accepted
        history = observer.release_history(oid)
    else:
        history = history_for(source)
    baseline = replay_release_history(history)
    assert baseline.consistent
    replay = replay_release_history(_rename_ending(history, target))
    if {source, target} == {"binding_stale", "scope_suspended"}:
        # r6: 归档无结束时 scope 快照，无法区分；不影响发布状态与结论。
        assert history["samples"] == []
        assert replay.consistent
        assert replay.integrity == ()
        assert replay.stored_status == baseline.stored_status
        assert replay.replayed_status == baseline.replayed_status
        assert replay.verdict == baseline.verdict == "unknown"
        return
    assert not replay.consistent, (source, target, replay)
    assert replay.verdict == "unknown"
    assert "ENDING_MISMATCH" in replay.integrity


@pytest.mark.parametrize("target", ["binding_stale", "scope_suspended"])
def test_real_pause_ending_cannot_be_relabelled_as_independent_revocation(
    harness, target
):
    web, observer, oid, _, _ = harness
    authorize(harness)
    assert (
        web.control_release(
            oid,
            action="pause",
            actor="human",
            expected_control_generation=0,
            idempotency_key="rename-independent-revocation",
            calibrated_policy_revision=None,
        ).outcome
        == "applied"
    )
    history = observer.release_history(oid)
    assert replay_release_history(history).consistent
    replay = replay_release_history(_rename_ending(history, target))
    assert not replay.consistent
    assert replay.verdict == "unknown"
    assert "ENDING_MISMATCH" in replay.integrity


def test_deadline_ending_with_sample_reference_is_42501(harness):
    _, observer, oid, _, _ = harness
    sid = authorize(harness).session_id
    receipt = observer.submit_release_sample(
        claim(harness, sid),
        raw_evidence(observer.release_context(sid), observer.current_time()),
    )
    assert receipt.accepted and receipt.ending_id is None
    expire(oid)
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as raised:
        with psycopg.connect(observer.dsn, row_factory=dict_row) as conn:
            row = release_row(conn, oid)
            eid = end_release_session(
                conn,
                row,
                session_row(conn, sid),
                reason="deadline_expired",
                status_after="unknown",
                sample_id=receipt.sample_id,
            )
            deadline_audit(conn, row, sid=sid, eid=eid)
            terminal_update(conn, oid)
    assert raised.value.sqlstate == "42501"


def test_deadline_ending_with_anomaly_key_is_already_rejected(harness):
    _, observer, oid, _, _ = harness
    sid = authorize(harness).session_id
    expire(oid)
    # 0015's immediate endings_anomaly CHECK already enforces this edge.
    with pytest.raises(psycopg.errors.CheckViolation) as raised:
        with psycopg.connect(observer.dsn, row_factory=dict_row) as conn:
            row = release_row(conn, oid)
            session = session_row(conn, sid)
            eid = uuid5(oid, "session-ending:" + str(sid))
            conn.execute(
                "INSERT INTO opspilot_observation_endings(ending_id,session_id,release_observation_id,ended_reason,transition,subject_state_before,subject_state_after,job_id,job_sequence,anomaly_key) VALUES(%s,%s,%s,'deadline_expired','release_unknown','observing','unknown',%s,%s,%s)",
                (
                    eid,
                    sid,
                    oid,
                    session["active_sample_job_id"],
                    session["active_sample_sequence"],
                    "foreign:" + str(oid),
                ),
            )
            conn.execute(
                "UPDATE opspilot_observation_sessions SET state='expired',ended_reason='deadline_expired',active_sample_job_id=NULL,active_sample_sequence=NULL,active_sample_due_at=NULL,active_sample_owner=NULL,active_sample_lease_until=NULL,updated_at=clock_timestamp() WHERE session_id=%s",
                (sid,),
            )
            deadline_audit(conn, row, sid=sid, eid=eid)
            terminal_update(conn, oid)
    assert raised.value.sqlstate == "23514"


@pytest.mark.parametrize("active_session", [False, True])
@pytest.mark.parametrize("reason", ["late_delivery", "max_samples_exhausted"])
def test_deadline_subject_reason_must_match_terminal_evidence(
    harness, active_session, reason
):
    _, observer, oid, _, _ = harness
    sid = authorize(harness).session_id if active_session else None
    expire(oid)
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as raised:
        with psycopg.connect(observer.dsn, row_factory=dict_row) as conn:
            row = release_row(conn, oid)
            eid = (
                end_release_session(
                    conn,
                    row,
                    session_row(conn, sid),
                    reason="deadline_expired",
                    status_after="unknown",
                )
                if sid
                else None
            )
            deadline_audit(conn, row, sid=sid, eid=eid)
            conn.execute(
                "UPDATE opspilot_release_observations SET status='unknown',ended_reason=%s,ended_at=clock_timestamp(),updated_at=clock_timestamp() WHERE observation_id=%s",
                (reason, oid),
            )
    assert raised.value.sqlstate == "42501"


def _snapshot_mutations(snapshot):
    """Change every saved result field independently, keeping its row intact."""
    for field, value in snapshot.items():
        if field == "_lease":
            continue
        if field.endswith("_id"):
            yield field, str(uuid4())
            if value is not None:
                yield field, None
        elif isinstance(value, bool):
            yield field, not value
        elif isinstance(value, (int, float)):
            yield field, value + 1
        elif field.endswith("_at"):
            yield field, "2000-01-01T00:00:00Z"
        elif field == "evaluation":
            yield (
                field,
                {
                    **value,
                    "verdict": "unknown"
                    if value["verdict"] != "unknown"
                    else "degraded",
                },
            )
        elif field == "session_state":
            yield field, "authorized" if value != "authorized" else "revoked"
        elif field in ("status", "release_status"):
            yield field, "cancelled" if value != "cancelled" else "unknown"
        elif field == "disposition":
            yield field, "history_only" if value == "adopted" else "adopted"
        elif field == "outcome":
            yield field, "rejected" if value == "applied" else "applied"
        else:
            yield field, "altered"
        # Removing a saved field must not turn it into an unchecked default.
        yield field, _MISSING


_MISSING = object()


def _assert_snapshot_tampering_rejected(history, rows, column):
    baseline = replay_release_history(history)
    assert baseline.consistent, baseline.integrity
    missed = []
    for index, row in enumerate(rows):
        for field, value in _snapshot_mutations(row[column]):
            corrupted = copy.deepcopy(history)
            snapshot = corrupted["samples" if column == "release_receipt" else "audit"][
                index
            ][column]
            if value is _MISSING:
                del snapshot[field]
            else:
                snapshot[field] = value
            replay = replay_release_history(corrupted)
            if replay.consistent or replay.verdict != "unknown":
                missed.append((index, field, "missing" if value is _MISSING else value))
            if field == "ending_id" and not replay.consistent:
                assert "ENDING_MISMATCH" in replay.integrity, replay.integrity
        if column == "release_receipt":
            for field, value in _snapshot_mutations(row[column]["_lease"]):
                corrupted = copy.deepcopy(history)
                lease = corrupted["samples"][index][column]["_lease"]
                if value is _MISSING:
                    del lease[field]
                else:
                    lease[field] = value
                replay = replay_release_history(corrupted)
                if replay.consistent or replay.verdict != "unknown":
                    missed.append(
                        (
                            index,
                            "_lease." + field,
                            "missing" if value is _MISSING else value,
                        )
                    )
    assert not missed, missed


@pytest.mark.parametrize(
    "mode",
    [
        "adopted",
        "scope_submit",
        "scope_claim",
        "scope_old_return",
        "paused_return",
        "lease_revoked",
    ],
)
def test_all_sample_receipt_fields_match_pg_history(harness, mode):
    web, observer, oid, _, _ = harness
    sid = authorize(harness).session_id
    context = observer.release_context(sid)
    lease = claim(harness, sid)
    owner = DurableStore(DSN)
    try:
        if mode in ("scope_submit", "scope_claim", "scope_old_return"):
            if mode == "scope_old_return":
                # Adopt sequence 1 and queue sequence 2, then let a different
                # sample from the old lease arrive after suspension. The
                # ending records the current slot, not this old return's job.
                first = observer.submit_release_sample(
                    lease, raw_evidence(context, observer.current_time())
                )
                assert first.accepted and first.ending_id is None
            owner.set_target_suspension(
                web.get_release(oid).target_id,
                True,
                expected_generation=0,
                actor="tester",
            )
            if mode == "scope_claim":
                with psycopg.connect(DSN) as conn:
                    conn.execute(
                        "UPDATE opspilot_observation_sessions SET active_sample_lease_until=clock_timestamp()-interval '1 second' WHERE session_id=%s",
                        (sid,),
                    )
                assert observer._claim_release_due(uuid4(), sid, 120) == []
        elif mode == "paused_return":
            web.control_release(
                oid,
                action="pause",
                actor="tester",
                expected_control_generation=0,
                idempotency_key="pause",
                calibrated_policy_revision=None,
            )
        elif mode == "lease_revoked":
            # A second owner reclaims the expired slot; the old return is history-only.
            with psycopg.connect(DSN) as conn:
                conn.execute(
                    "UPDATE opspilot_observation_sessions SET active_sample_lease_until=clock_timestamp()-interval '1 second' WHERE session_id=%s",
                    (sid,),
                )
            assert observer._claim_release_due(uuid4(), sid, 120)
        observer.submit_release_sample(
            lease, raw_evidence(context, observer.current_time())
        )
        history = observer.release_history(oid)
        _assert_snapshot_tampering_rejected(
            history, history["samples"], "release_receipt"
        )
    finally:
        owner.close()


@pytest.mark.parametrize(
    ("reason", "harness"),
    [("healthy_confirmed", 40), ("anomaly_detected", 40), ("max_samples_exhausted", 2)],
    indirect=["harness"],
)
def test_all_terminal_sample_receipt_fields_match_pg_history(harness, reason):
    _, observer, oid, _, _ = harness
    sid = authorize(harness).session_id
    context = observer.release_context(sid)
    if reason != "max_samples_exhausted":
        time.sleep(2.05)
    receipt = observer.submit_release_sample(
        claim(harness, sid),
        raw_evidence(
            context, observer.current_time(), degraded=reason == "anomaly_detected"
        ),
    )
    if reason != "anomaly_detected":
        if reason == "healthy_confirmed":
            time.sleep(1.05)
        receipt = observer.submit_release_sample(
            claim(harness, sid), raw_evidence(context, observer.current_time())
        )
    assert receipt.ending_id is not None
    history = observer.release_history(oid)
    _assert_snapshot_tampering_rejected(history, history["samples"], "release_receipt")


@pytest.mark.parametrize(
    "action", ["authorize", "pause", "resume", "cancel", "rejected", "deadline_expired"]
)
def test_all_audit_result_fields_match_pg_history(harness, action):
    web, observer, oid, policy, _ = harness
    if action == "deadline_expired":
        # Keep received_at before deadline: move the frozen test clock before authorization.
        with psycopg.connect(DSN) as conn:
            conn.execute("SET LOCAL session_replication_role=replica")
            conn.execute(
                "UPDATE opspilot_release_observations SET released_at=statement_timestamp()-(deadline_seconds-1)*interval '1 second',deadline_at=statement_timestamp()+interval '1 second' WHERE observation_id=%s",
                (oid,),
            )
    sid = authorize(harness).session_id
    if action == "deadline_expired":
        time.sleep(1.05)
        assert oid in observer.sweep_expired_releases(limit=10000)
    elif action != "authorize":
        observer.submit_release_sample(
            claim(harness, sid),
            raw_evidence(observer.release_context(sid), observer.current_time()),
        )
        if action == "resume":
            owner = DurableStore(DSN)
            generation = owner.set_target_suspension(
                web.get_release(oid).target_id,
                True,
                expected_generation=0,
                actor="tester",
            )
            owner.set_target_suspension(
                web.get_release(oid).target_id,
                False,
                expected_generation=generation,
                actor="tester",
            )
            owner.close()
        if action == "rejected":
            web.authorize_release(
                oid,
                actor="tester",
                expected_control_generation=0,
                idempotency_key="rejected",
                calibrated_policy_revision=policy.revision,
            )
        else:
            web.control_release(
                oid,
                action=action,
                actor="tester",
                expected_control_generation=0,
                idempotency_key=action,
                calibrated_policy_revision=policy.revision
                if action == "resume"
                else None,
            )
    assert sid is not None
    history = observer.release_history(oid)
    _assert_snapshot_tampering_rejected(history, history["audit"], "result")
