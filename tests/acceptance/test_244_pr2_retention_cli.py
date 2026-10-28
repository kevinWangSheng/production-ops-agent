"""#244 PR-2: retention through its documented CLI and report interface.

The only application interface imported below is the public ``run_retention``
contract. Fixture SQL creates upstream records; observations use the CLI JSON
lines and read-only SQL against the stored records.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.persistence import DurableStore, close_pools
from scripts.m0.postgres_lab import DSN
from tests.f13_acceptance_support import seed_upstream

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

TABLES = (
    "opspilot_evidence",
    "opspilot_observation_signal_readings",
    "opspilot_release_query_results",
    "opspilot_release_comparison_queries",
    "opspilot_alert_deliveries",
)
ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def _retention_login(admin_dsn: str):
    """The contract creates a login outside the product and grants the group."""
    name = f"retention_accept_{uuid4().hex[:8]}"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(name)))
        conn.execute(
            sql.SQL("GRANT opspilot_retention TO {}").format(sql.Identifier(name))
        )
    try:
        yield make_conninfo(admin_dsn, user=name)
    finally:
        close_pools()
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename=%s",
                (name,),
            )
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))


@pytest.fixture
def db():
    name = f"retention_pr2_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    dsn = make_conninfo(DSN, dbname=name)
    try:
        schema.migrate(dsn, pg_dump=os.environ.get("OPSPILOT_PG_DUMP", "pg_dump"))
        yield dsn
    finally:
        close_pools()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


def _close(dsn: str, incident: UUID, *, old: bool = True) -> None:
    store = DurableStore(dsn)
    try:
        generation = store.control(incident, 0, "close", "acceptance")
        assert generation == 1
    finally:
        store.close()
    if old:
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "UPDATE opspilot_incidents SET closed_at=clock_timestamp()-interval '31 days' "
                "WHERE incident_id=%s",
                (incident,),
            )
            closed_at = conn.execute(
                "SELECT closed_at FROM opspilot_incidents WHERE incident_id=%s",
                (incident,),
            ).fetchone()[0]
            assert closed_at < datetime.now(timezone.utc) - timedelta(days=30)


def _incident(dsn: str, *, old: bool = True) -> UUID:
    incident = seed_upstream(dsn)
    _close(dsn, incident, old=old)
    return incident


def _evidence(dsn: str, incident: UUID) -> str:
    return f"ev-{incident}"


def _present_reading(dsn: str, incident: UUID) -> str:
    raw = b'{"status":"success"}'
    digest = hashlib.sha256(raw).hexdigest()
    with psycopg.connect(dsn) as conn:
        sample = conn.execute(
            "SELECT m.sample_id FROM opspilot_observation_samples m "
            "JOIN opspilot_observation_sessions s USING(session_id) "
            "WHERE s.incident_id=%s ORDER BY m.sequence LIMIT 1",
            (incident,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO opspilot_observation_signal_readings("
            "sample_id,signal_name,status,value,sample_count,query,window_start,window_end,"
            "source,raw_sha256,raw) SELECT sample_id,'retention-payload',status,value,"
            "sample_count,query,window_start,window_end,source,%s,%s "
            "FROM opspilot_observation_signal_readings WHERE sample_id=%s LIMIT 1",
            (digest, raw, sample),
        )
    return f"{sample}:retention-payload"


def _alert(dsn: str, incident: UUID | None) -> str:
    raw = '{"alerts":[{"status":"firing"}]}'
    digest = hashlib.sha256(raw.encode()).hexdigest()
    key = uuid4().hex
    with psycopg.connect(dsn) as conn:
        delivery = conn.execute(
            "INSERT INTO opspilot_alert_deliveries("
            "delivery_key,fingerprint,starts_at,starts_at_raw,status,outcome,incident_id,"
            "actor,raw_sha256,annotations_sha256,annotation_revision,alert_json,truncated) "
            "VALUES(%s,%s,clock_timestamp(),%s,'firing','created',%s,'acceptance',"
            "%s,%s,1,%s,false) RETURNING delivery_id",
            (
                key,
                key,
                datetime.now(timezone.utc).isoformat(),
                incident,
                digest,
                digest,
                raw,
            ),
        ).fetchone()[0]
    return str(delivery)


def _release_rows(dsn: str, incident: UUID | None) -> dict[str, str]:
    """Minimal valid release graph; triggers requiring Observer leases are fixture-only bypassed."""
    observation, session, sample, ending, comparison = (uuid4() for _ in range(5))
    target = uuid4()
    profile_id = f"p-{uuid4().hex[:8]}"
    content = json.dumps({"profile_id": profile_id})
    profile_hash = hashlib.sha256(content.encode()).hexdigest()
    profile = f"{profile_id}@{profile_hash[:12]}"
    now = datetime.now(timezone.utc) - timedelta(days=2)
    raw = b'{"status":"success"}'
    digest = hashlib.sha256(raw).hexdigest()
    revision = {
        "images": {"service": "sha256:" + "a" * 64},
        "pod_template_digest": "sha256:" + "b" * 64,
    }
    with psycopg.connect(dsn) as conn:
        conn.execute("SET session_replication_role = 'replica'")
        conn.execute(
            "INSERT INTO opspilot_targets(target_id,resource_uid) VALUES(%s,%s)",
            (target, f"t-{target}"),
        )
        conn.execute(
            "INSERT INTO opspilot_health_profiles(health_profile_revision,profile_id,content_sha256,content) "
            "VALUES(%s,%s,%s,%s)",
            (profile, profile_id, profile_hash, content),
        )
        conn.execute(
            "INSERT INTO opspilot_release_observations("
            "observation_id,format_version,source,source_release_id,target_id,target_snapshot,"
            "before_revision,after_revision,auxiliary,semantic_sha256,health_profile_revision,"
            "timing_policy_version,released_at,deadline_at,deadline_seconds,min_tracking_seconds,"
            "sample_interval_seconds,sustained_window_seconds,max_samples,status,ended_reason,ended_at) "
            "VALUES(%s,1,'acceptance',%s,%s,'{}',%s::jsonb,%s::jsonb,'{}',%s,%s,1,"
            "%s,%s,3600,60,30,60,2,'anomalous','anomaly_detected',%s)",
            (
                observation,
                str(observation),
                target,
                json.dumps(revision),
                json.dumps(revision),
                "c" * 64,
                profile,
                now,
                now + timedelta(hours=1),
                now + timedelta(minutes=5),
            ),
        )
        conn.execute(
            "INSERT INTO opspilot_observation_sessions("
            "session_id,purpose,release_observation_id,authorization_source,subject_kind,subject_id,target_id,target,"
            "subject_control_generation,observation_generation,authorized_by,health_profile_revision,"
            "release_policy_revision,release_budget_used_at_authorization,"
            "authorized_global_generation,authorized_target_generation,deadline_at,"
            "max_samples,sample_interval_seconds,sustained_window_seconds,state,ended_reason) "
            "VALUES(%s,'release_observation',%s,NULL,'release_observation',%s,%s,'{}',0,0,"
            "'acceptance',%s,%s,0,0,0,%s,2,30,60,'completed','anomaly_detected')",
            (
                session,
                observation,
                observation,
                target,
                profile,
                profile,
                now + timedelta(hours=1),
            ),
        )
        conn.execute(
            "INSERT INTO opspilot_observation_samples("
            "sample_id,session_id,job_id,sequence,epoch,window_start,window_end,outcome,"
            "required_signals_present,subject_control_generation,observation_generation,"
            "health_profile_revision,disposition,reason,confirms_health,health_basis,"
            "scope_suspended,global_generation,target_generation,within_deadline,lease_valid,"
            "lease_stamps_match,readings_consistent,purpose,subject_kind,subject_id,"
            "current_subject_state,current_subject_control_generation,"
            "current_subject_observation_generation,decision_at,release_evidence_sha256,"
            "release_evaluation,release_receipt) "
            "VALUES(%s,%s,%s,1,1,%s,%s,'degraded',true,0,0,%s,'history_only',"
            "'readings_inconsistent',false,'outcome_not_healthy',false,0,0,true,true,true,"
            "true,'release_observation','release_observation',%s,'anomalous',0,0,"
            "%s,%s,'{}','{}')",
            (
                sample,
                session,
                uuid4(),
                now,
                now + timedelta(minutes=5),
                profile,
                observation,
                now + timedelta(minutes=5),
                digest,
            ),
        )
        conn.execute(
            "INSERT INTO opspilot_observation_endings("
            "ending_id,session_id,purpose,release_observation_id,subject_kind,subject_id,"
            "subject_state_before,subject_state_after,ended_reason,transition,sample_id,anomaly_key) "
            "VALUES(%s,%s,'release_observation',%s,'release_observation',%s,"
            "'observing','anomalous','anomaly_detected','release_anomalous',%s,%s)",
            (
                ending,
                session,
                observation,
                observation,
                sample,
                f"release-anomaly:{observation}",
            ),
        )
        conn.execute(
            "INSERT INTO opspilot_release_query_results("
            "sample_id,query_key,query,source,evaluated_at,status,detail,body_complete,raw,raw_sha256,payload_state) "
            "VALUES(%s,'sample-query','up','prometheus',%s,'ok','',true,%s,%s,'present')",
            (sample, now, raw, digest),
        )
        owner = uuid4()
        conn.execute(
            "INSERT INTO opspilot_release_comparison_attempts("
            "comparison_id,attempt,observation_id,ending_id,owner,lease_until) "
            "VALUES(%s,1,%s,%s,%s,%s)",
            (comparison, observation, ending, owner, now + timedelta(hours=1)),
        )
        comparison_json = {
            "status": "complete",
            "comparison_id": str(comparison),
            "ending_id": str(ending),
            "observation_id": str(observation),
            "sample_id": str(sample),
            "attempts": 1,
        }
        conn.execute(
            "INSERT INTO opspilot_release_comparisons("
            "comparison_id,observation_id,ending_id,session_id,sample_id,owner,"
            "format_version,status,attempts,window_before_start,window_before_end,"
            "window_after_start,window_after_end,decision_at,evidence_sha256,comparison) "
            "VALUES(%s,%s,%s,%s,%s,%s,1,'complete',1,%s,%s,%s,%s,%s,%s,%s::jsonb)",
            (
                comparison,
                observation,
                ending,
                session,
                sample,
                owner,
                now - timedelta(minutes=5),
                now,
                now,
                now + timedelta(minutes=5),
                now + timedelta(minutes=5),
                digest,
                json.dumps(comparison_json),
            ),
        )
        conn.execute(
            "INSERT INTO opspilot_release_comparison_queries("
            "comparison_id,query_key,query,source,evaluated_at,status,detail,body_complete,raw,raw_sha256,payload_state) "
            "VALUES(%s,'comparison-query','up','prometheus',%s,'ok','',true,%s,%s,'present')",
            (comparison, now, raw, digest),
        )
        if incident is not None:
            conn.execute(
                "INSERT INTO opspilot_release_incident_links("
                "observation_id,ending_id,anomaly_key,disposition,incident_id,run_id,comparison_id) "
                "VALUES(%s,%s,%s,'created',%s,%s,%s)",
                (
                    observation,
                    ending,
                    f"release-anomaly:{observation}",
                    incident,
                    _run_id(conn, incident),
                    comparison,
                ),
            )
        conn.execute("SET session_replication_role = 'origin'")
    return {
        "query": f"{sample}:sample-query",
        "comparison": f"{comparison}:comparison-query",
    }


def _run_id(conn: psycopg.Connection, incident: UUID) -> UUID:
    return conn.execute(
        "SELECT current_run_id FROM opspilot_incidents WHERE incident_id=%s",
        (incident,),
    ).fetchone()[0]


def _snapshot(dsn: str, refs: dict[str, str]) -> dict[str, tuple]:
    specs = {
        "opspilot_evidence": (
            "evidence_id",
            "raw,raw_sha256,payload_state,view,view_sha256,observed_at",
        ),
        "opspilot_observation_signal_readings": (
            "sample_id::text||':'||signal_name",
            "raw,raw_sha256,payload_state,window_start,window_end,query",
        ),
        "opspilot_release_query_results": (
            "sample_id::text||':'||query_key",
            "raw,raw_sha256,payload_state,evaluated_at,query,body_complete",
        ),
        "opspilot_release_comparison_queries": (
            "comparison_id::text||':'||query_key",
            "raw,raw_sha256,payload_state,evaluated_at,query,body_complete",
        ),
        "opspilot_alert_deliveries": (
            "delivery_id::text",
            "alert_json,raw_sha256,payload_state,received_at,truncated",
        ),
    }
    result = {}
    with psycopg.connect(dsn) as conn:
        for table, ident in refs.items():
            key, cols = specs[table]
            row = conn.execute(
                f"SELECT {cols} FROM {table} WHERE {key}=%s", (ident,)
            ).fetchone()
            assert row is not None, (table, ident)
            count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            result[table] = (row, count)
    return result


def _lines(report) -> list[dict]:
    assert isinstance(report.lines, tuple)
    assert all(isinstance(line, dict) for line in report.lines)
    assert report.lines[-1]["kind"] == "summary"
    return list(report.lines)


def _run(dsn: str, **kwargs):
    from opspilot.retention import run_retention

    return run_retention(dsn, **kwargs)


def _cli(dsn: str | None, *args: str) -> tuple[int, list[dict], str]:
    env = os.environ.copy()
    env.pop("OPSPILOT_RETENTION_DSN", None)
    if dsn is not None:
        env["OPSPILOT_RETENTION_DSN"] = dsn
    proc = subprocess.run(
        [sys.executable, "-m", "opspilot.retention", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    lines = [json.loads(line) for line in proc.stdout.splitlines() if line]
    return proc.returncode, lines, proc.stderr


def test_pr2_fixture_backdate_keeps_closed_at_and_present_payload(db: str) -> None:
    """Existing PR-1 guard: a closed row may be backdated without lifecycle rewrite."""
    incident = _incident(db)
    assert _evidence(db, incident)
    with psycopg.connect(db) as conn:
        closed_at, state = conn.execute(
            "SELECT closed_at,lifecycle FROM opspilot_incidents WHERE incident_id=%s",
            (incident,),
        ).fetchone()
        assert state == "closed"
        assert closed_at < datetime.now(timezone.utc) - timedelta(days=30)
        assert conn.execute(
            "SELECT payload_state FROM opspilot_evidence WHERE evidence_id=%s",
            (_evidence(db, incident),),
        ).fetchone() == ("present",)


def test_pr2_ac1_default_dry_run_and_apply_five_payloads_via_process(db: str) -> None:
    """AC1: five candidates, no dry-run writes, then exact payload erasure."""
    incident = _incident(db)
    refs = {
        TABLES[0]: _evidence(db, incident),
        TABLES[1]: _present_reading(db, incident),
        TABLES[4]: _alert(db, incident),
    }
    release = _release_rows(db, incident)
    refs[TABLES[2]], refs[TABLES[3]] = release["query"], release["comparison"]
    before = _snapshot(db, refs)
    with _retention_login(db) as retention:
        rc, lines, stderr = _cli(retention)
        assert rc == 0, stderr
        assert lines[-1] == {
            "kind": "summary",
            "apply": False,
            "days": 30,
            "candidates": 5,
            "cleaned": 0,
            "failed": 0,
            "blocked": 0,
        }
        candidates = [line for line in lines if line["kind"] == "candidate"]
        assert {(line["table"], line["object_id"]) for line in candidates} == set(
            refs.items()
        )
        assert all(
            line["incident_id"] == str(incident) and line["raw_sha256"]
            for line in candidates
        )
        assert _snapshot(db, refs) == before
        rc, lines, stderr = _cli(retention, "--apply")
        assert rc == 0, stderr
        assert lines[-1] == {
            "kind": "summary",
            "apply": True,
            "days": 30,
            "candidates": 5,
            "cleaned": 5,
            "failed": 0,
            "blocked": 0,
        }
        assert {
            (line["table"], line["object_id"])
            for line in lines
            if line["kind"] == "cleaned"
        } == set(refs.items())
    after = _snapshot(db, refs)
    for table in TABLES:
        prior, count = before[table]
        updated, new_count = after[table]
        assert new_count == count
        assert updated[0] is None and updated[2] == "cleaned"
        assert updated[1] == prior[1] and updated[3:] == prior[3:]


@pytest.mark.parametrize(
    "case,code",
    [
        ("not_closed", "NOT_CLOSED"),
        ("recent", "WITHIN_RETENTION"),
        ("missing", "CLOSED_AT_MISSING"),
        ("reopened", "WITHIN_RETENTION"),
        ("active_run", "ACTIVE_RUN"),
        ("active_observation", "ACTIVE_OBSERVATION"),
        ("generation", "POSTMORTEM_GENERATION_PENDING"),
        ("review", "DRAFT_UNDER_REVIEW"),
        ("knowledge", "ACTIVE_KNOWLEDGE"),
    ],
)
def test_pr2_ac2_blocked_incident_reports_reason_and_keeps_payload(
    db: str,
    case: str,
    code: str,
) -> None:
    """AC2: each documented retention dependency blocks one incident."""
    incident = _incident(db, old=case not in {"recent", "reopened"})
    evidence = _evidence(db, incident)
    if case == "not_closed":
        with psycopg.connect(db) as conn:
            conn.execute(
                "UPDATE opspilot_incidents SET lifecycle='open',state='investigating' "
                "WHERE incident_id=%s",
                (incident,),
            )
    elif case == "missing":
        with psycopg.connect(db) as conn:
            conn.execute(
                "UPDATE opspilot_incidents SET closed_at=NULL WHERE incident_id=%s",
                (incident,),
            )
    elif case == "reopened":
        store = DurableStore(db)
        try:
            assert store.control(incident, 1, "reopen", "acceptance") == 2
            assert store.control(incident, 2, "close", "acceptance") == 3
        finally:
            store.close()
    elif case == "active_run":
        with psycopg.connect(db) as conn:
            conn.execute(
                "INSERT INTO opspilot_runs(run_id,incident_id,state,control_generation,budget_limit,deadline,versions) "
                "VALUES(%s,%s,'queued',1,1,clock_timestamp()+interval '1 hour','{}')",
                (uuid4(), incident),
            )
    elif case == "active_observation":
        with psycopg.connect(db) as conn:
            conn.execute(
                "UPDATE opspilot_observation_sessions SET state='authorized',ended_reason=NULL "
                "WHERE incident_id=%s",
                (incident,),
            )
    elif case == "generation":
        with psycopg.connect(db) as conn:
            conn.execute(
                "INSERT INTO opspilot_postmortem_generation_jobs(incident_id,pending_regeneration_version) "
                "VALUES(%s,1)",
                (incident,),
            )
    elif case in {"review", "knowledge"}:
        _f13_blocker(db, incident, case)
    with _retention_login(db) as retention:
        lines = _lines(_run(retention))
        blocked = [
            line
            for line in lines
            if line["kind"] == "blocked" and line["incident_id"] == str(incident)
        ]
        assert len(blocked) == 1 and code in blocked[0]["blocked_by"]
        assert not any(
            line["kind"] == "candidate" and line.get("incident_id") == str(incident)
            for line in lines
        )
        _run(retention, apply=True)
    with psycopg.connect(db) as conn:
        assert conn.execute(
            "SELECT raw,payload_state FROM opspilot_evidence WHERE evidence_id=%s",
            (evidence,),
        ).fetchone() == (b"\x00", "present")


def _f13_blocker(dsn: str, incident: UUID, case: str) -> None:
    """Insert a valid F13 provenance graph; audit guards are outside fixture scope."""
    postmortem, entry, event = uuid4(), uuid4(), uuid4()
    content = "{}"
    digest = hashlib.sha256(content.encode()).hexdigest()
    with psycopg.connect(dsn) as conn:
        conn.execute("SET session_replication_role = 'replica'")
        conn.execute(
            "INSERT INTO opspilot_postmortems(postmortem_id,incident_id,latest_version) VALUES(%s,%s,1)",
            (postmortem, incident),
        )
        session, ending = conn.execute(
            "SELECT s.session_id,e.ending_id FROM opspilot_observation_sessions s "
            "JOIN opspilot_observation_endings e USING(session_id) WHERE s.incident_id=%s LIMIT 1",
            (incident,),
        ).fetchone()
        conn.execute(
            "INSERT INTO opspilot_postmortem_versions("
            "postmortem_id,version,state,content,content_sha256,incident_control_generation,"
            "observation_generation,observation_session_id,observation_ending_id,run_count,"
            "last_run_id,input_watermark,evidence_snapshot_sha256,generated_by,generated_by_kind) "
            "VALUES(%s,1,%s,%s,%s,1,1,%s,%s,1,%s,0,%s,'acceptance','worker')",
            (
                postmortem,
                "under_review" if case == "review" else "approved",
                content,
                digest,
                session,
                ending,
                _run_id(conn, incident),
                digest,
            ),
        )
        if case == "knowledge":
            conn.execute(
                "INSERT INTO opspilot_knowledge_entries(entry_id,latest_revision) VALUES(%s,1)",
                (entry,),
            )
            conn.execute(
                "INSERT INTO opspilot_postmortem_proposals("
                "postmortem_id,version,proposal_key,name,tags,content,content_sha256) "
                "VALUES(%s,1,'p','probe',ARRAY['probe'],%s,%s)",
                (postmortem, content, digest),
            )
            conn.execute(
                "INSERT INTO opspilot_knowledge_revisions("
                "entry_id,revision,name,tags,content,content_sha256,source_postmortem_id,"
                "source_version,source_proposal_key,approved_by,approved_by_kind,event_id) "
                "VALUES(%s,1,'probe',ARRAY['probe'],%s,%s,%s,1,'p','acceptance','basic_auth',%s)",
                (entry, content, digest, postmortem, event),
            )
        conn.execute("SET session_replication_role = 'origin'")


def test_pr2_ac2_unattributed_alert_and_release_rows_stay_present(db: str) -> None:
    """AC2/r3-A: rows without an incident owner cannot age out."""
    alert = _alert(db, None)
    release = _release_rows(db, None)
    refs = {
        TABLES[2]: release["query"],
        TABLES[3]: release["comparison"],
        TABLES[4]: alert,
    }
    before = _snapshot(db, refs)
    with _retention_login(db) as retention:
        lines = _lines(_run(retention))
        assert not any(line.get("object_id") in refs.values() for line in lines)
        _run(retention, apply=True)
    assert _snapshot(db, refs) == before


def test_pr2_ac2_merge_group_with_recent_member_blocks_all(db: str) -> None:
    """AC2/r3-B: a young merged member retains the whole group."""
    old, recent = _incident(db), _incident(db, old=False)
    refs = {TABLES[0]: _evidence(db, old)}
    before = _snapshot(db, refs)
    with psycopg.connect(db) as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET merged_into_incident_id=%s WHERE incident_id=%s",
            (old, recent),
        )
    with _retention_login(db) as retention:
        lines = _lines(_run(retention))
        blocked = [line for line in lines if line["kind"] == "blocked"]
        assert {str(old), str(recent)} <= {line["incident_id"] for line in blocked}
        assert any(
            line["incident_id"] == str(old)
            and "MERGE_GROUP_BLOCKED" in line["blocked_by"]
            for line in blocked
        )
        _run(retention, apply=True)
    assert _snapshot(db, refs) == before


@pytest.mark.parametrize("change", ["reopen", "new_run"])
def test_pr2_ac3_change_after_scan_blocks_cleanup(db: str, change: str) -> None:
    """AC3: the scan result is rechecked after a human lifecycle/Run change."""
    incident = _incident(db)
    refs = {TABLES[0]: _evidence(db, incident)}
    before = _snapshot(db, refs)
    called = 0

    def after_scan() -> None:
        nonlocal called
        called += 1
        if change == "reopen":
            store = DurableStore(db)
            try:
                assert store.control(incident, 1, "reopen", "acceptance") == 2
            finally:
                store.close()
        else:
            with psycopg.connect(db) as conn:
                conn.execute(
                    "INSERT INTO opspilot_runs(run_id,incident_id,state,control_generation,budget_limit,deadline,versions) "
                    "VALUES(%s,%s,'queued',1,1,clock_timestamp()+interval '1 hour','{}')",
                    (uuid4(), incident),
                )

    with _retention_login(db) as retention:
        lines = _lines(_run(retention, apply=True, after_scan=after_scan))
    assert called == 1
    assert any(
        line["kind"] == "blocked"
        and line["incident_id"] == str(incident)
        and "CHANGED_AFTER_SCAN" in line["blocked_by"]
        for line in lines
    )
    assert _snapshot(db, refs) == before


def test_pr2_ac4_one_failed_row_does_not_rollback_other_incident(db: str) -> None:
    """AC4: a row-specific SQL failure is reported; another incident commits."""
    failing, good = _incident(db), _incident(db)
    failure_id, good_id = _evidence(db, failing), _evidence(db, good)
    with psycopg.connect(db) as conn:
        conn.execute("""
            CREATE FUNCTION retention_fixture_failure() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF NEW.evidence_id = TG_ARGV[0] AND NEW.raw IS NULL THEN
                RAISE EXCEPTION 'fixture_row_failure';
              END IF;
              RETURN NEW;
            END $$
        """)
        conn.execute(
            sql.SQL(
                "CREATE TRIGGER retention_fixture_failure BEFORE UPDATE ON opspilot_evidence "
                "FOR EACH ROW EXECUTE FUNCTION retention_fixture_failure({})"
            ).format(sql.Literal(failure_id))
        )
    try:
        with _retention_login(db) as retention:
            report = _run(retention, apply=True)
            lines = _lines(report)
            assert report.failed == 1
            assert any(
                line["kind"] == "failed"
                and line["object_id"] == failure_id
                and "fixture_row_failure" in line["error"]
                for line in lines
            )
            assert any(
                line["kind"] == "cleaned" and line["object_id"] == good_id
                for line in lines
            )
            rc, cli_lines, _ = _cli(retention, "--apply")
            # The first run already cleaned the good row. The failing row still causes exit 1.
            assert rc == 1
            assert any(
                line["kind"] == "failed" and line["object_id"] == failure_id
                for line in cli_lines
            )
        with psycopg.connect(db) as conn:
            assert conn.execute(
                "SELECT payload_state FROM opspilot_evidence WHERE evidence_id=%s",
                (failure_id,),
            ).fetchone() == ("present",)
            assert conn.execute(
                "SELECT payload_state FROM opspilot_evidence WHERE evidence_id=%s",
                (good_id,),
            ).fetchone() == ("cleaned",)
    finally:
        with psycopg.connect(db) as conn:
            conn.execute("DROP TRIGGER retention_fixture_failure ON opspilot_evidence")
            conn.execute("DROP FUNCTION retention_fixture_failure()")


@pytest.mark.parametrize("days", ["0", "-1", "29", "abc"])
def test_pr2_ac5_short_retention_is_config_error_with_no_writes(
    db: str, days: str
) -> None:
    """AC5: only 30 days or longer is accepted by the process entry."""
    incident = _incident(db)
    refs = {TABLES[0]: _evidence(db, incident)}
    before = _snapshot(db, refs)
    with _retention_login(db) as retention:
        rc, lines, stderr = _cli(retention, "--apply", "--days", days)
    assert rc == 2 and "RETENTION_DAYS_INVALID" in stderr
    assert not any(line["kind"] == "cleaned" for line in lines)
    assert _snapshot(db, refs) == before


def test_pr2_ac5_library_rejects_short_days(db: str) -> None:
    """AC5/r3-D: the documented library seam rejects an invalid minimum."""
    with _retention_login(db) as retention:
        with pytest.raises(ValueError, match="^RETENTION_DAYS_INVALID$"):
            _run(retention, days=29, apply=True)


def test_pr2_ac5_missing_dsn_is_config_error() -> None:
    """AC5/r3-D: no process credentials means exit 2."""
    rc, lines, _ = _cli(None)
    assert rc == 2 and not any(line["kind"] == "cleaned" for line in lines)
