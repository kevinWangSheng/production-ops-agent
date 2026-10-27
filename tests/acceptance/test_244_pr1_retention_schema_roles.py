"""Issue #244 PR-1 acceptance tests.

These tests are written against the migration, storage Protocols, and the
database roles. They intentionally do not import the implementation branch's
retention CLI or inspect its private call order.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Iterator
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.persistence import DurableStore
from scripts.m0.postgres_lab import DSN
from tests.runtime_roles_support import catalog_grants, login, relations

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required",
)

BEFORE_RETENTION = "0021_release_incident_links"
RETENTION_HEAD = "0023_retention_raw_cleanup"


@pytest.fixture
def scratch_dsn() -> Iterator[str]:
    name = f"opspilot_244_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    dsn = make_conninfo(DSN, dbname=name)
    try:
        yield dsn
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


def _upgrade_before_retention(dsn: str) -> None:
    """Create the historical shape used by the migration backfill test."""
    schema.upgrade_to(dsn, BEFORE_RETENTION)


def _upgrade_head(dsn: str) -> None:
    # The expected revision is a contract value. The implementation may
    # temporarily point 0023 at 0021 while 0022 is being merged, so this test
    # never asserts 0023's down_revision.
    schema.upgrade_head(dsn)
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            RETENTION_HEAD,
        )


def _seed_observer_graph(
    dsn: str, *, raw: bytes | None, incident: UUID | None = None
) -> dict[str, UUID | str]:
    """Insert one valid pre-0023 Observer reading using only old columns."""
    incident = incident or uuid4()
    target = uuid4()
    profile_id = f"profile-{uuid4().hex[:8]}"
    profile_content = json.dumps({"profile_id": profile_id}, separators=(",", ":"))
    profile_sha = hashlib.sha256(profile_content.encode()).hexdigest()
    profile_revision = f"{profile_id}@{profile_sha[:12]}"
    session = uuid4()
    sample = uuid4()
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=2)
    end = now - timedelta(minutes=1)
    raw_sha = hashlib.sha256(raw).hexdigest() if raw is not None else None
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO opspilot_targets(target_id,resource_uid) VALUES(%s,%s)",
            (target, f"target-{target}"),
        )
        conn.execute(
            "INSERT INTO opspilot_incidents(incident_id,intake_key,state,lifecycle,target_id) "
            "VALUES(%s,%s,'queued','open',%s)",
            (incident, f"intake-{incident}", target),
        )
        conn.execute(
            "INSERT INTO opspilot_health_profiles(health_profile_revision,profile_id,content_sha256,content) "
            "VALUES(%s,%s,%s,%s)",
            (profile_revision, profile_id, profile_sha, profile_content),
        )
        conn.execute(
            "INSERT INTO opspilot_observation_sessions("
            "session_id,incident_id,purpose,target_id,target,subject_control_generation,"
            "observation_generation,authorized_by,health_profile_revision,"
            "authorized_global_generation,authorized_target_generation,deadline_at,"
            "max_samples,sample_interval_seconds,sustained_window_seconds,subject_kind,subject_id) "
            "VALUES(%s,%s,'incident_recovery',%s,%s::jsonb,0,0,'acceptance',%s,0,0,%s,2,30,30,'incident',%s)",
            (
                session,
                incident,
                target,
                json.dumps({"resource_uid": f"target-{target}"}),
                profile_revision,
                now + timedelta(hours=1),
                incident,
            ),
        )
        conn.execute(
            "INSERT INTO opspilot_observation_samples("
            "sample_id,session_id,job_id,sequence,epoch,window_start,window_end,outcome,"
            "required_signals_present,subject_control_generation,observation_generation,"
            "health_profile_revision,disposition,reason,confirms_health,health_basis,"
            "subject_lifecycle,incident_control_generation,incident_observation_generation,"
            "scope_suspended,global_generation,target_generation,within_deadline,lease_valid,"
            "lease_stamps_match,readings_consistent,purpose,subject_kind,subject_id,"
            "current_subject_state,current_subject_control_generation,current_subject_observation_generation) "
            "VALUES(%s,%s,%s,1,1,%s,%s,'no_data',true,0,0,%s,'history_only',"
            "'readings_inconsistent',false,'outcome_not_healthy','open',0,0,false,0,0,true,true,true,true,"
            "'incident_recovery','incident',%s,'open',0,0)",
            (sample, session, uuid4(), start, end, profile_revision, incident),
        )
        conn.execute(
            "INSERT INTO opspilot_observation_signal_readings("
            "sample_id,signal_name,status,value,sample_count,query,window_start,window_end,source,raw_sha256,raw) "
            "VALUES(%s,'error_ratio','no_data',NULL,NULL,'up',%s,%s,'prometheus',%s,%s)",
            (sample, start, end, raw_sha, raw),
        )
    return {
        "incident": incident,
        "target": target,
        "session": session,
        "sample": sample,
        "profile": profile_revision,
    }


def _seed_release_sample_for_query(dsn: str) -> UUID:
    """Seed the parent identity needed by the existing Observer query INSERT."""
    sample = uuid4()
    session = uuid4()
    job = uuid4()
    now = datetime.now(timezone.utc)
    with psycopg.connect(dsn) as conn:
        # The F11 release contract owns construction of a complete release
        # session. This fixture isolates the query-row insert itself.
        conn.execute("SET session_replication_role = 'replica'")
        conn.execute(
            "INSERT INTO opspilot_observation_samples("
            "sample_id,session_id,job_id,sequence,epoch,window_start,window_end,outcome,"
            "required_signals_present,subject_control_generation,observation_generation,"
            "health_profile_revision,disposition,reason,confirms_health,health_basis,"
            "scope_suspended,global_generation,target_generation,within_deadline,lease_valid,"
            "lease_stamps_match,readings_consistent,purpose,subject_kind,subject_id,"
            "current_subject_state,current_subject_control_generation,current_subject_observation_generation,"
            "decision_at,release_evidence_sha256,release_evaluation,release_receipt) "
            "VALUES(%s,%s,%s,1,1,%s,%s,'degraded',true,0,0,'revision@000000000000',"
            "'history_only','readings_inconsistent',false,'outcome_not_healthy',false,0,0,true,true,true,true,"
            "'release_observation','release_observation',%s,'pending',0,0,%s,%s,%s::jsonb,%s::jsonb)",
            (
                sample,
                session,
                job,
                now - timedelta(minutes=2),
                now - timedelta(minutes=1),
                uuid4(),
                now,
                "a" * 64,
                json.dumps({"verdict": "degraded"}),
                json.dumps({"reason": "fixture"}),
            ),
        )
        conn.execute("SET session_replication_role = 'origin'")
    return sample


def _seed_evidence(dsn: str, *, raw: bytes = b"evidence") -> str:
    evidence_id = f"e-{uuid4()}"
    digest = hashlib.sha256(raw).hexdigest()
    view = {"status": "ok", "rows": 1}
    view_sha = hashlib.sha256(
        json.dumps(view, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO opspilot_evidence("
            "evidence_id,run_id,subject_id,status,adopted,raw,raw_sha256,view,view_sha256,"
            "projection_revision,observed_at) VALUES(%s,%s,%s,'ok',true,%s,%s,%s::jsonb,%s,'test',clock_timestamp())",
            (
                evidence_id,
                str(uuid4()),
                str(uuid4()),
                raw,
                digest,
                json.dumps(view),
                view_sha,
            ),
        )
    return evidence_id


def test_pr1_migration_backfills_historical_observer_states_and_reaches_named_head(
    scratch_dsn: str,
) -> None:
    """PR-1: historical empty Observer raw is absent; payload is present."""
    _upgrade_before_retention(scratch_dsn)
    empty = _seed_observer_graph(scratch_dsn, raw=None)
    present = _seed_observer_graph(scratch_dsn, raw=b"historical payload")
    _upgrade_head(scratch_dsn)
    with psycopg.connect(scratch_dsn) as conn:
        rows = conn.execute(
            "SELECT sample_id,raw,raw_sha256,payload_state "
            "FROM opspilot_observation_signal_readings ORDER BY sample_id"
        ).fetchall()
        by_sample = {row[0]: row[1:] for row in rows}
        assert by_sample[empty["sample"]] == (None, None, "absent")
        assert by_sample[present["sample"]][0] == b"historical payload"
        assert (
            by_sample[present["sample"]][1]
            == hashlib.sha256(b"historical payload").hexdigest()
        )
        assert by_sample[present["sample"]][2] == "present"
        assert (
            conn.execute(
                "SELECT count(*) FROM opspilot_observation_signal_readings WHERE payload_state='cleaned'"
            ).fetchone()[0]
            == 0
        )


def test_pr1_existing_seed_observer_and_payload_writes_survive_the_new_state_column(
    scratch_dsn: str,
) -> None:
    """PR-1: raw-null SQL/Observer writes and existing payload writes remain valid."""
    _upgrade_before_retention(scratch_dsn)
    graph = _seed_observer_graph(scratch_dsn, raw=None)
    _upgrade_head(scratch_dsn)

    # The historical direct SQL shape deliberately omits payload_state.
    direct = _seed_observer_graph(scratch_dsn, raw=None)
    with login(scratch_dsn, "opspilot_observer") as observer_dsn:
        # A login member of the real Observer group uses the existing public
        # INSERT shape and still gets an absent state for raw=NULL.
        with psycopg.connect(observer_dsn) as conn:
            conn.execute(
                "INSERT INTO opspilot_observation_signal_readings("
                "sample_id,signal_name,status,query,window_start,window_end,source,raw_sha256,raw) "
                "VALUES(%s,'observer-null','no_data','up',clock_timestamp()-interval '2 minutes',"
                "clock_timestamp()-interval '1 minute','prometheus',NULL,NULL)",
                (graph["sample"],),
            )
            observer_raw = b'{"observer":"ok"}'
            conn.execute(
                "INSERT INTO opspilot_observation_signal_readings("
                "sample_id,signal_name,status,value,sample_count,query,window_start,window_end,source,raw_sha256,raw) "
                "VALUES(%s,'observer-raw','ok',1.0,1,'up',clock_timestamp()-interval '2 minutes',"
                "clock_timestamp()-interval '1 minute','prometheus',%s,%s)",
                (
                    graph["sample"],
                    hashlib.sha256(observer_raw).hexdigest(),
                    observer_raw,
                ),
            )
            release_sample = _seed_release_sample_for_query(scratch_dsn)
            release_raw = b'{"release":"ok"}'
            conn.execute(
                "INSERT INTO opspilot_release_query_results("
                "sample_id,query_key,query,source,evaluated_at,status,detail,body_complete,raw,raw_sha256) "
                "VALUES(%s,'query','up','prometheus',clock_timestamp(),'ok','',true,%s,%s)",
                (release_sample, release_raw, hashlib.sha256(release_raw).hexdigest()),
            )

    with psycopg.connect(scratch_dsn) as conn:
        assert (
            conn.execute(
                "SELECT payload_state FROM opspilot_observation_signal_readings "
                "WHERE sample_id=%s AND signal_name='observer-null'",
                (graph["sample"],),
            ).fetchone()[0]
            == "absent"
        )
        assert (
            conn.execute(
                "SELECT payload_state FROM opspilot_observation_signal_readings "
                "WHERE sample_id=%s AND signal_name='observer-raw'",
                (graph["sample"],),
            ).fetchone()[0]
            == "present"
        )
        assert (
            conn.execute(
                "SELECT payload_state FROM opspilot_release_query_results WHERE sample_id=%s",
                (release_sample,),
            ).fetchone()[0]
            == "present"
        )
        assert (
            conn.execute(
                "SELECT payload_state FROM opspilot_observation_signal_readings WHERE sample_id=%s",
                (direct["sample"],),
            ).fetchone()[0]
            == "absent"
        )

        # These are the existing insertion shapes: non-null payloads must be
        # accepted and become present. Alert and evidence rows use the owner
        # path exactly as their current stores do.
        evidence_id = _seed_evidence(scratch_dsn)
        alert = {"status": "firing", "alerts": []}
        alert_bytes = json.dumps(alert, separators=(",", ":")).encode()
        alert_sha = hashlib.sha256(alert_bytes).hexdigest()
        conn.execute(
            "INSERT INTO opspilot_alert_deliveries("
            "delivery_key,fingerprint,starts_at,starts_at_raw,status,outcome,incident_id,actor,"
            "raw_sha256,annotations_sha256,annotation_revision,alert_json,truncated) "
            "VALUES(%s,%s,clock_timestamp(),%s,'firing','created',%s,'acceptance',%s,%s,1,%s,false)",
            (
                f"delivery-{uuid4()}",
                "fingerprint",
                "2026-10-27T00:00:00Z",
                graph["incident"],
                alert_sha,
                alert_sha,
                alert_bytes.decode(),
            ),
        )
        assert (
            conn.execute(
                "SELECT payload_state FROM opspilot_alert_deliveries ORDER BY delivery_id DESC LIMIT 1"
            ).fetchone()[0]
            == "present"
        )
        assert (
            conn.execute(
                "SELECT payload_state FROM opspilot_evidence WHERE evidence_id=%s",
                (evidence_id,),
            ).fetchone()[0]
            == "present"
        )


def test_pr1_web_worker_and_observer_catalog_is_unchanged_by_retention_migration(
    scratch_dsn: str,
) -> None:
    """PR-1: the three existing runtime authorization directories stay equal."""
    _upgrade_before_retention(scratch_dsn)
    roles = ("opspilot_web", "opspilot_worker", "opspilot_observer")
    with psycopg.connect(scratch_dsn) as conn:
        before = {role: catalog_grants(conn, role) for role in roles}
    schema.upgrade_head(scratch_dsn)
    with psycopg.connect(scratch_dsn) as conn:
        after = {role: catalog_grants(conn, role) for role in roles}
    for role in roles:
        for relation, grants in before[role].items():
            assert after[role][relation] == grants, (role, relation)


def test_pr1_close_reopen_and_close_again_use_database_closed_at_clock(
    scratch_dsn: str,
) -> None:
    """PR-1: close records DB time, reopen clears it, and close restarts it."""
    _upgrade_head(scratch_dsn)
    store = DurableStore(scratch_dsn)
    try:
        incident = uuid4()
        run = uuid4()
        store.accept(
            incident,
            run,
            f"close-{incident}",
            deadline=datetime.now(timezone.utc) + timedelta(hours=1),
            budget_limit=10,
            versions={"state": "v1"},
        )
        assert store.control(incident, 0, "close", "acceptance") == 1
        with psycopg.connect(scratch_dsn) as conn:
            first, db_now = conn.execute(
                "SELECT closed_at,clock_timestamp() FROM opspilot_incidents WHERE incident_id=%s",
                (incident,),
            ).fetchone()
        assert first is not None and first <= db_now
        assert store.control(incident, 1, "reopen", "acceptance") == 2
        with psycopg.connect(scratch_dsn) as conn:
            assert (
                conn.execute(
                    "SELECT closed_at FROM opspilot_incidents WHERE incident_id=%s",
                    (incident,),
                ).fetchone()[0]
                is None
            )
        assert store.control(incident, 2, "close", "acceptance") == 3
        with psycopg.connect(scratch_dsn) as conn:
            second = conn.execute(
                "SELECT closed_at FROM opspilot_incidents WHERE incident_id=%s",
                (incident,),
            ).fetchone()[0]
        assert second is not None and second > first
    finally:
        store.close()


def test_pr1_retention_role_has_only_declared_columns_and_can_clean_one_row(
    scratch_dsn: str,
) -> None:
    """PR-1: retention login is least privilege and its guarded update works."""
    _upgrade_head(scratch_dsn)
    evidence_id = _seed_evidence(scratch_dsn)
    from opspilot.persistence.grants import RETENTION_GRANTS, RUNTIME_GRANTS

    # New retention grants must be a separate declaration. The existing
    # runtime matrix remains the source of truth for web and worker parity.
    with psycopg.connect(scratch_dsn) as conn:
        present = relations(conn)
        for role, declared in RUNTIME_GRANTS.items():
            assert catalog_grants(conn, role) == declared
        actual = catalog_grants(conn, "opspilot_retention")
        declared = RETENTION_GRANTS
        # The retention declaration may omit explicit empty entries; every
        # relation absent from it must still have an empty catalog grant.
        assert all(
            grants == declared.get(table, frozenset())
            for table, grants in actual.items()
        )
        for table, column in (
            ("opspilot_evidence", "raw"),
            ("opspilot_observation_signal_readings", "raw"),
            ("opspilot_release_query_results", "raw"),
            ("opspilot_release_comparison_queries", "raw"),
            ("opspilot_alert_deliveries", "alert_json"),
        ):
            assert table in present
            assert "SELECT" in actual[table]
            assert any(
                item.startswith("UPDATE(") and column in item for item in actual[table]
            )
        assert all(
            "DELETE" not in grants and "TRUNCATE" not in grants
            for grants in actual.values()
        )

    with login(scratch_dsn, "opspilot_retention") as retention_dsn:
        with psycopg.connect(retention_dsn) as conn:
            conn.execute(
                "UPDATE opspilot_evidence SET raw=NULL,payload_state='cleaned' WHERE evidence_id=%s",
                (evidence_id,),
            )
        for statement in (
            "UPDATE opspilot_evidence SET status='tampered' WHERE evidence_id=%s",
            "DELETE FROM opspilot_evidence WHERE evidence_id=%s",
            "TRUNCATE opspilot_evidence",
            "INSERT INTO opspilot_f13_audit(event_id,idempotency_key,object_kind,object_id,action,actor_id,principal_kind,expected_generation,resulting_generation) VALUES(gen_random_uuid(),'retention-denied','test',%s,'test','retention','retention',0,0)",
        ):
            with pytest.raises(psycopg.Error) as refused:
                with psycopg.connect(retention_dsn) as conn:
                    conn.execute(
                        statement, (evidence_id,) if "%s" in statement else None
                    )
            assert (
                getattr(refused.value, "sqlstate", None) == "42501"
                or "refus" in str(refused.value).lower()
            )


def test_pr1_retention_lock_only_grants_lock_rows_and_rejects_real_updates(
    scratch_dsn: str,
) -> None:
    """PR-1 r2: lock-only columns permit row locks but no data UPDATEs."""
    _upgrade_head(scratch_dsn)
    graph = _seed_observer_graph(scratch_dsn, raw=b"lock fixture")
    run_id = uuid4()
    with psycopg.connect(scratch_dsn) as conn:
        conn.execute(
            "INSERT INTO opspilot_runs(run_id,incident_id,state,control_generation,"
            "budget_limit,deadline,versions) VALUES(%s,%s,'queued',0,10,clock_timestamp()+interval '1 hour',%s::jsonb)",
            (run_id, graph["incident"], json.dumps({"state": "v1"})),
        )

    with login(scratch_dsn, "opspilot_retention") as retention_dsn:
        for statement, key in (
            (
                "SELECT incident_id FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
                graph["incident"],
            ),
            (
                "SELECT run_id FROM opspilot_runs WHERE run_id=%s FOR UPDATE",
                run_id,
            ),
            (
                "SELECT session_id FROM opspilot_observation_sessions WHERE session_id=%s FOR UPDATE",
                graph["session"],
            ),
        ):
            with psycopg.connect(retention_dsn) as conn:
                assert conn.execute(statement, (key,)).fetchone() is not None

        for statement, key in (
            (
                "UPDATE opspilot_incidents SET lifecycle=lifecycle WHERE incident_id=%s",
                graph["incident"],
            ),
            (
                "UPDATE opspilot_runs SET state=state WHERE run_id=%s",
                run_id,
            ),
            (
                "UPDATE opspilot_observation_sessions SET state=state WHERE session_id=%s",
                graph["session"],
            ),
        ):
            with pytest.raises(psycopg.Error) as refused:
                with psycopg.connect(retention_dsn) as conn:
                    conn.execute(statement, (key,))
            assert (
                getattr(refused.value, "sqlstate", None) == "42501"
                or "refus" in str(refused.value).lower()
            )

        for lock in ("FOR UPDATE", "FOR SHARE"):
            with pytest.raises(psycopg.Error) as refused:
                with psycopg.connect(retention_dsn) as conn:
                    conn.execute(f"SELECT event_id FROM opspilot_f13_audit {lock}")
            assert (
                getattr(refused.value, "sqlstate", None) == "42501"
                or "privilege" in str(refused.value).lower()
            )


def test_pr1_alembic_round_trip_and_cleaned_rows_block_downgrade(
    scratch_dsn: str,
) -> None:
    """PR-1: head round-trips; downgrade refuses while cleaned data exists."""
    _upgrade_head(scratch_dsn)
    schema.command.downgrade(schema._config(scratch_dsn), BEFORE_RETENTION)
    schema.upgrade_head(scratch_dsn)
    with psycopg.connect(scratch_dsn) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            RETENTION_HEAD,
        )

    evidence_id = _seed_evidence(scratch_dsn)
    with psycopg.connect(scratch_dsn) as conn:
        conn.execute(
            "UPDATE opspilot_evidence SET raw=NULL,payload_state='cleaned' WHERE evidence_id=%s",
            (evidence_id,),
        )
    with pytest.raises(Exception, match="cleaned|RAW|payload"):
        schema.command.downgrade(schema._config(scratch_dsn), BEFORE_RETENTION)
