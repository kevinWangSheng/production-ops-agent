"""#244 PR-1: retention schema, ``opspilot_retention`` role and guards (0023).

The lab database is at head (``conftest``). Covered: ``payload_state`` on the
five raw payload tables (backfill, insert paths, consistency), ``closed_at``
written and cleared by close / reopen, the ``opspilot_retention`` catalog
equal to ``RETENTION_GRANTS`` while web / worker / Observer stay what they
were at 0021, the restricted cleaning update (and every refusal around it),
the release immutability exemption, and the downgrade refusal while a
``cleaned`` row exists.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.observer.loop import ObserverLoop
from opspilot.persistence import DurableStore
from opspilot.persistence.grants import RETENTION_GRANTS, RETENTION_ROLE
from opspilot.web.evidence import DurableEvidenceStore
from scripts.m0.postgres_lab import DSN
from tests.f13_acceptance_support import seed_upstream
from tests.integration import test_f11_pr4a_comparison_impl_postgres as pr4a
from tests.runtime_roles_support import catalog_grants, login, relations

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

PG_DUMP = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")
BEFORE = "0021_release_incident_links"
release_harness = pr4a.release_harness
PAYLOAD_TABLES = {
    "opspilot_evidence": "raw",
    "opspilot_observation_signal_readings": "raw",
    "opspilot_release_query_results": "raw",
    "opspilot_release_comparison_queries": "raw",
    "opspilot_alert_deliveries": "alert_json",
}
ALERT_RAW = '{"status":"firing"}'


@pytest.fixture
def scratch() -> Iterator[str]:
    name = f"opspilot_ret_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    try:
        yield make_conninfo(DSN, dbname=name)
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


@pytest.fixture(scope="module")
def retention_dsn():
    with login(DSN, RETENTION_ROLE) as dsn:
        yield dsn


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _alert(conn, incident: UUID) -> int:
    return conn.execute(
        "INSERT INTO opspilot_alert_deliveries(delivery_key,fingerprint,starts_at,"
        "starts_at_raw,status,outcome,incident_id,actor,raw_sha256,annotations_sha256,"
        "annotation_revision,alert_json,truncated) VALUES(%s,'fp',now(),'t','firing',"
        "'created',%s,'am',%s,%s,1,%s,false) RETURNING delivery_id",
        (f"k-{uuid4()}", incident, "a" * 64, "b" * 64, ALERT_RAW),
    ).fetchone()[0]


def _seed(dsn: str) -> dict:
    """An incident with evidence, two Observer readings without raw (the
    shape of the legacy rows and of the direct seed path), one reading with
    raw and one alert delivery."""
    incident = seed_upstream(dsn)
    with psycopg.connect(dsn) as conn:
        sample, signal = conn.execute(
            "SELECT r.sample_id, r.signal_name FROM opspilot_observation_signal_readings r "
            "JOIN opspilot_observation_samples s USING (sample_id) "
            "JOIN opspilot_observation_sessions o ON o.session_id=s.session_id "
            "WHERE o.incident_id=%s ORDER BY s.sequence, r.signal_name LIMIT 1",
            (incident,),
        ).fetchone()
        raw = b'{"status":"success"}'
        conn.execute(
            "INSERT INTO opspilot_observation_signal_readings(sample_id,signal_name,status,"
            "value,sample_count,query,window_start,window_end,source,raw_sha256,raw) "
            "SELECT sample_id,'latency',status,value,sample_count,query,window_start,"
            "window_end,source,%s,%s FROM opspilot_observation_signal_readings "
            "WHERE sample_id=%s AND signal_name=%s",
            (_sha(raw), raw, sample, signal),
        )
        delivery = _alert(conn, incident)
    return {
        "incident": incident,
        "evidence": f"ev-{incident}",
        "sample": sample,
        "absent": signal,
        "present": "latency",
        "delivery": delivery,
    }


def _states(dsn: str, seeded: dict) -> dict[str, str]:
    with psycopg.connect(dsn) as conn:
        return {
            "evidence": conn.execute(
                "SELECT payload_state FROM opspilot_evidence WHERE evidence_id=%s",
                (seeded["evidence"],),
            ).fetchone()[0],
            "absent": conn.execute(
                "SELECT payload_state FROM opspilot_observation_signal_readings "
                "WHERE sample_id=%s AND signal_name=%s",
                (seeded["sample"], seeded["absent"]),
            ).fetchone()[0],
            "present": conn.execute(
                "SELECT payload_state FROM opspilot_observation_signal_readings "
                "WHERE sample_id=%s AND signal_name=%s",
                (seeded["sample"], seeded["present"]),
            ).fetchone()[0],
            "alert": conn.execute(
                "SELECT payload_state FROM opspilot_alert_deliveries WHERE delivery_id=%s",
                (seeded["delivery"],),
            ).fetchone()[0],
        }


# --- migration: backfill and round trip


def test_upgrade_backfills_absent_and_present_and_never_cleaned(scratch) -> None:
    schema.command.upgrade(schema._config(scratch), BEFORE)
    seeded = _seed(scratch)
    with psycopg.connect(scratch) as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET lifecycle='closed' WHERE incident_id=%s",
            (seeded["incident"],),
        )
    schema.command.upgrade(schema._config(scratch), "head")
    assert _states(scratch, seeded) == {
        "evidence": "present",
        "absent": "absent",
        "present": "present",
        "alert": "present",
    }
    with psycopg.connect(scratch) as conn:
        for table in PAYLOAD_TABLES:
            assert (
                conn.execute(
                    sql.SQL(
                        "SELECT count(*) FROM {} WHERE payload_state='cleaned'"
                    ).format(sql.Identifier(table))
                ).fetchone()[0]
                == 0
            )
        # an incident closed before the migration has no reliable close time
        assert conn.execute(
            "SELECT lifecycle, closed_at FROM opspilot_incidents WHERE incident_id=%s",
            (seeded["incident"],),
        ).fetchone() == ("closed", None)


def test_runtime_catalogs_are_the_same_at_0021_and_at_head(scratch) -> None:
    schema.command.upgrade(schema._config(scratch), BEFORE)
    roles = ("opspilot_web", "opspilot_worker", "opspilot_observer")
    with psycopg.connect(scratch) as conn:
        before = {role: catalog_grants(conn, role) for role in roles}
    schema.command.upgrade(schema._config(scratch), "head")
    with psycopg.connect(scratch) as conn:
        assert {role: catalog_grants(conn, role) for role in roles} == before


def test_downgrade_round_trips_and_refuses_while_a_row_is_cleaned(scratch) -> None:
    schema.migrate(scratch, pg_dump=PG_DUMP)
    head = schema.schema_dump(scratch, pg_dump=PG_DUMP)
    seeded = _seed(scratch)
    schema.command.downgrade(schema._config(scratch), BEFORE)
    assert schema.schema_dump(scratch, pg_dump=PG_DUMP) == schema.fresh_dump(
        scratch, BEFORE, pg_dump=PG_DUMP
    )
    schema.command.upgrade(schema._config(scratch), "head")
    assert schema.schema_dump(scratch, pg_dump=PG_DUMP) == head
    with login(scratch, RETENTION_ROLE) as dsn, psycopg.connect(dsn) as conn:
        conn.execute(
            "UPDATE opspilot_evidence SET raw=NULL,payload_state='cleaned' "
            "WHERE evidence_id=%s",
            (seeded["evidence"],),
        )
    with pytest.raises(RuntimeError, match="cleaned"):
        schema.command.downgrade(schema._config(scratch), BEFORE)
    with psycopg.connect(scratch) as conn:
        assert schema.current_revision(conn) == schema.head_revision()


def test_an_existing_retention_role_that_can_log_in_fails_the_upgrade(scratch) -> None:
    """The role is cluster-wide and already exists (the lab database is at
    head); LOGIN is set for the duration of the test only."""
    schema.command.upgrade(schema._config(scratch), BEFORE)
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"ALTER ROLE {RETENTION_ROLE} LOGIN")
    try:
        with pytest.raises(Exception, match="not a plain NOLOGIN group .*LOGIN"):
            schema.command.upgrade(schema._config(scratch), "head")
        with psycopg.connect(scratch) as conn:
            assert schema.current_revision(conn) == BEFORE
            assert conn.execute(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE column_name IN ('payload_state','closed_at')"
            ).fetchone() == (0,)
        with psycopg.connect(DSN) as conn:
            assert conn.execute(
                "SELECT rolcanlogin FROM pg_roles WHERE rolname=%s", (RETENTION_ROLE,)
            ).fetchone() == (True,)
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"ALTER ROLE {RETENTION_ROLE} NOLOGIN")


# --- insert paths


def test_existing_insert_paths_keep_working_and_raw_null_is_not_present() -> None:
    seeded = _seed(DSN)
    assert _states(DSN, seeded) == {
        "evidence": "present",
        "absent": "absent",
        "present": "present",
        "alert": "present",
    }
    with psycopg.connect(DSN) as conn:
        # a writer cannot claim a state: the insert trigger derives it
        conn.execute(
            "INSERT INTO opspilot_observation_signal_readings(sample_id,signal_name,status,"
            "value,sample_count,query,window_start,window_end,source,payload_state) "
            "SELECT sample_id,'forged',status,value,sample_count,query,window_start,"
            "window_end,source,'present' FROM opspilot_observation_signal_readings "
            "WHERE sample_id=%s AND signal_name=%s",
            (seeded["sample"], seeded["absent"]),
        )
        assert conn.execute(
            "SELECT payload_state FROM opspilot_observation_signal_readings "
            "WHERE sample_id=%s AND signal_name='forged'",
            (seeded["sample"],),
        ).fetchone() == ("absent",)


@pytest.mark.parametrize("table", ["opspilot_evidence", "opspilot_alert_deliveries"])
def test_the_four_formerly_not_null_payloads_still_refuse_a_null_insert(table) -> None:
    seeded = _seed(DSN)
    with psycopg.connect(DSN) as conn:
        with pytest.raises(psycopg.errors.NotNullViolation):
            if table == "opspilot_evidence":
                conn.execute(
                    "INSERT INTO opspilot_evidence(evidence_id,run_id,subject_id,status,"
                    "adopted,raw,raw_sha256,view,view_sha256,projection_revision,observed_at) "
                    "SELECT %s,run_id,subject_id,status,adopted,NULL,raw_sha256,view,"
                    "view_sha256,projection_revision,observed_at FROM opspilot_evidence "
                    "WHERE evidence_id=%s",
                    (f"ev-{uuid4()}", seeded["evidence"]),
                )
            else:
                conn.execute(
                    "INSERT INTO opspilot_alert_deliveries(delivery_key,fingerprint,"
                    "starts_at,starts_at_raw,status,outcome,actor,raw_sha256,"
                    "annotations_sha256,annotation_revision,alert_json,truncated) "
                    "VALUES('k','fp',now(),'t','firing','created','am',%s,%s,1,NULL,false)",
                    ("a" * 64, "b" * 64),
                )


def test_stored_evidence_reads_present_rows_unchanged() -> None:
    seeded = _seed(DSN)
    stored = DurableEvidenceStore(DurableStore(DSN)).get(seeded["evidence"])
    assert stored is not None
    assert stored.raw == b"\x00" and stored.payload_state == "present"


# --- closed_at


def _closed_at(incident: UUID):
    with psycopg.connect(DSN) as conn:
        return conn.execute(
            "SELECT closed_at FROM opspilot_incidents WHERE incident_id=%s", (incident,)
        ).fetchone()[0]


def _db_now():
    with psycopg.connect(DSN) as conn:
        return conn.execute("SELECT clock_timestamp()").fetchone()[0]


def test_close_writes_closed_at_reopen_clears_and_close_again_restarts() -> None:
    incident = seed_upstream(DSN)
    with login(DSN, "opspilot_web") as dsn:
        store = DurableStore(dsn)
        assert _closed_at(incident) is None
        t0 = _db_now()
        assert store.control(incident, 0, "close", "operator") == 1
        first = _closed_at(incident)
        assert first is not None and t0 <= first <= _db_now()
        # a later update that keeps the lifecycle keeps the close time
        with psycopg.connect(DSN) as conn:
            conn.execute(
                "UPDATE opspilot_incidents SET priority=1 WHERE incident_id=%s",
                (incident,),
            )
        assert _closed_at(incident) == first
        assert store.control(incident, 1, "reopen", "operator") == 2
        assert _closed_at(incident) is None
        assert store.control(incident, 2, "close", "operator") == 3
        second = _closed_at(incident)
        assert second is not None and second > first


# --- the retention role


def test_retention_catalog_equals_the_declaration_and_the_role_is_a_bare_group() -> (
    None
):
    with psycopg.connect(DSN) as conn:
        assert set(RETENTION_GRANTS) == relations(conn)
        assert catalog_grants(conn, RETENTION_ROLE) == RETENTION_GRANTS
        attributes = conn.execute(
            "SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolreplication, "
            "rolbypassrls, rolinherit FROM pg_roles WHERE rolname=%s",
            (RETENTION_ROLE,),
        ).fetchone()
        assert attributes[:6] == (False,) * 6
        assert conn.execute(
            "SELECT count(*) FROM pg_class WHERE relowner=%s::regrole",
            (RETENTION_ROLE,),
        ).fetchone() == (0,)


def test_retention_cleans_the_three_runtime_tables_and_nothing_else(
    retention_dsn,
) -> None:
    seeded = _seed(DSN)
    with psycopg.connect(retention_dsn) as conn:
        conn.execute(
            "UPDATE opspilot_evidence SET raw=NULL,payload_state='cleaned' "
            "WHERE evidence_id=%s",
            (seeded["evidence"],),
        )
        conn.execute(
            "UPDATE opspilot_observation_signal_readings SET raw=NULL,"
            "payload_state='cleaned' WHERE sample_id=%s AND signal_name=%s",
            (seeded["sample"], seeded["present"]),
        )
        conn.execute(
            "UPDATE opspilot_alert_deliveries SET alert_json=NULL,"
            "payload_state='cleaned' WHERE delivery_id=%s",
            (seeded["delivery"],),
        )
    states = _states(DSN, seeded)
    assert states == {
        "evidence": "cleaned",
        "absent": "absent",
        "present": "cleaned",
        "alert": "cleaned",
    }
    with psycopg.connect(DSN) as conn:
        assert conn.execute(
            "SELECT raw IS NULL, raw_sha256, view_sha256 FROM opspilot_evidence "
            "WHERE evidence_id=%s",
            (seeded["evidence"],),
        ).fetchone() == (True, "0" * 64, "1" * 64)
    stored = DurableEvidenceStore(DurableStore(DSN)).get(seeded["evidence"])
    assert stored is not None and stored.raw is None
    assert stored.payload_state == "cleaned" and stored.hashes_verified is None


@pytest.mark.parametrize(
    "statement",
    [
        # another column
        "UPDATE opspilot_evidence SET status='error' WHERE evidence_id=%(ev)s",
        "UPDATE opspilot_evidence SET raw_sha256='x' WHERE evidence_id=%(ev)s",
        # the payload column to anything but NULL, or the state alone
        "UPDATE opspilot_evidence SET raw='\\x01' WHERE evidence_id=%(ev)s",
        "UPDATE opspilot_evidence SET raw='\\x01',payload_state='cleaned' WHERE evidence_id=%(ev)s",
        "UPDATE opspilot_evidence SET payload_state='absent',raw=NULL WHERE evidence_id=%(ev)s",
        # an absent reading is not cleaned
        "UPDATE opspilot_observation_signal_readings SET payload_state='cleaned' "
        "WHERE sample_id=%(sample)s AND signal_name=%(absent)s",
        "DELETE FROM opspilot_evidence WHERE evidence_id=%(ev)s",
        "DELETE FROM opspilot_alert_deliveries WHERE delivery_id=%(delivery)s",
        "TRUNCATE opspilot_evidence",
        "TRUNCATE opspilot_observation_signal_readings",
        "UPDATE opspilot_incidents SET closed_at=NULL WHERE incident_id=%(incident)s",
        "UPDATE opspilot_postmortems SET generation=generation",
        "INSERT INTO opspilot_knowledge_entries(entry_id) VALUES (gen_random_uuid())",
        "INSERT INTO opspilot_f13_audit(event_id) VALUES (gen_random_uuid())",
        "SELECT * FROM opspilot_steps",
        "SELECT * FROM opspilot_inputs",
    ],
)
def test_retention_refuses_everything_but_the_cleaning_update(
    retention_dsn, statement
) -> None:
    seeded = _seed(DSN)
    params = {
        "ev": seeded["evidence"],
        "sample": seeded["sample"],
        "absent": seeded["absent"],
        "delivery": seeded["delivery"],
        "incident": seeded["incident"],
    }
    with psycopg.connect(retention_dsn) as conn:
        with pytest.raises(
            (psycopg.errors.InsufficientPrivilege, psycopg.errors.CheckViolation)
        ):
            conn.execute(statement, params)


def test_a_cleaned_payload_is_never_restored(retention_dsn) -> None:
    seeded = _seed(DSN)
    with psycopg.connect(retention_dsn) as conn:
        conn.execute(
            "UPDATE opspilot_evidence SET raw=NULL,payload_state='cleaned' "
            "WHERE evidence_id=%s",
            (seeded["evidence"],),
        )
    with psycopg.connect(retention_dsn) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "UPDATE opspilot_evidence SET raw='\\x00',payload_state='present' "
                "WHERE evidence_id=%s",
                (seeded["evidence"],),
            )


def test_release_tables_stay_immutable_except_the_retention_cleaning(
    release_harness, retention_dsn
) -> None:
    _, observer, oid, _, _ = release_harness
    context = pr4a._anomalous(release_harness)
    ObserverLoop(
        store=observer, source=pr4a.BeforeTelemetry(context), batch=1
    ).poll_once()
    (comparison,) = pr4a._comparisons(observer, oid)
    with psycopg.connect(DSN) as conn:
        sample, key = conn.execute(
            "SELECT q.sample_id, q.query_key FROM opspilot_release_query_results q "
            "JOIN opspilot_observation_samples s USING (sample_id) "
            "JOIN opspilot_observation_sessions o ON o.session_id=s.session_id "
            "WHERE o.release_observation_id=%s ORDER BY 1, 2 LIMIT 1",
            (oid,),
        ).fetchone()
        ckey = conn.execute(
            "SELECT query_key FROM opspilot_release_comparison_queries "
            "WHERE comparison_id=%s ORDER BY 1 LIMIT 1",
            (comparison["comparison_id"],),
        ).fetchone()[0]
        assert conn.execute(
            "SELECT payload_state FROM opspilot_release_query_results "
            "WHERE sample_id=%s AND query_key=%s",
            (sample, key),
        ).fetchone() == ("present",)
    query = (
        "UPDATE opspilot_release_query_results SET {} WHERE sample_id=%s AND query_key=%s",
        (sample, key),
    )
    compared = (
        "UPDATE opspilot_release_comparison_queries SET {} "
        "WHERE comparison_id=%s AND query_key=%s",
        (comparison["comparison_id"], ckey),
    )
    for template, args in (query, compared):
        # the owner is held to append-only too, and so is any other edit
        for dsn, change in (
            (DSN, "raw=NULL,payload_state='cleaned'"),
            (retention_dsn, "raw=NULL,payload_state='cleaned',detail='x'"),
            (retention_dsn, "raw='\\x00',payload_state='cleaned'"),
        ):
            with psycopg.connect(dsn) as conn:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(template.format(change), args)
        with psycopg.connect(retention_dsn) as conn:
            assert (
                conn.execute(
                    template.format("raw=NULL,payload_state='cleaned'"), args
                ).rowcount
                == 1
            )
        # cleaned once is final: not even the state alone again
        with psycopg.connect(retention_dsn) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(template.format("payload_state='cleaned'"), args)
    with psycopg.connect(DSN) as conn:
        assert conn.execute(
            "SELECT raw IS NULL, payload_state, raw_sha256 IS NOT NULL "
            "FROM opspilot_release_query_results WHERE sample_id=%s AND query_key=%s",
            (sample, key),
        ).fetchone() == (True, "cleaned", True)
        for statement in (
            "DELETE FROM opspilot_release_query_results",
            "TRUNCATE opspilot_release_comparison_queries",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    conn.execute(statement)
