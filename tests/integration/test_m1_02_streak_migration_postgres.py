"""Migration 0006 (#157, user decision B 2026-10-08) on real PostgreSQL.

C3 section 10: "规则变化后，如仍获授权则创建新版本观察会话，否则保持停止".
The rule for the sustained healthy window changed, and a migration cannot
authorize on a human's behalf, so 0006 ends every session that is still
``authorized`` (``authority_revoked``, lifecycle unchanged, like a pause)
and leaves every ended session's record alone. A human then registers the
remediation again, which opens a new session that accumulates under the
new rule. The data step is the migration's own function, run here on rows
the store wrote; the Observer login shows the old session is no longer
claimable.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.domain.evidence import QueryWindow
from opspilot.domain.observation import HealthSample
from opspilot.observation import ObservationStore, SignalReading
from opspilot.persistence import DurableStore, PoolConfig
from scripts.m0.postgres_lab import DSN
from tests.integration.test_m1_02_observation_store_postgres import (
    PROFILE,
    PROFILE_CONTENT,
    _backdate_authorization,
    _incident,
)

MIGRATION = (
    Path(schema.__file__).parent
    / "migrations/versions/0006_healthy_streak_window_end.py"
)
_spec = importlib.util.spec_from_file_location("migration_0006", MIGRATION)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
end_open_sessions = _module.end_open_sessions

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

PG_DUMP = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")
POOL = PoolConfig(min_size=1, max_size=3, timeout=5.0)


@pytest.fixture(scope="module")
def scratch_dsn() -> Iterator[str]:
    name = f"opspilot_streak_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    dsn = make_conninfo(DSN, dbname=name)
    schema.migrate(dsn, pg_dump=PG_DUMP)
    try:
        yield dsn
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


@pytest.fixture(scope="module")
def observer_dsn(scratch_dsn: str) -> Iterator[str]:
    login = f"opspilot_observer_login_{uuid4().hex[:8]}"
    with psycopg.connect(scratch_dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN IN ROLE opspilot_observer").format(
                sql.Identifier(login)
            )
        )
    try:
        yield make_conninfo(scratch_dsn, user=login)
    finally:
        with psycopg.connect(scratch_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(login)))


def _now() -> datetime:
    return datetime.now(timezone.utc)  # noqa: TID251 - test clock


def _authorize(controller: ObservationStore, incident: UUID, revision: str) -> UUID:
    session = controller.authorize_session(
        incident,
        revision=revision,
        actor="tester",
        deadline_at=_now() + timedelta(hours=1),
        max_samples=10,
        sample_interval_seconds=15,
        sustained_window_seconds=3600,
        health_profile_revision=PROFILE,
        health_profile=PROFILE_CONTENT,
        first_sample_due_at=_now(),
    )
    # the window must start after the authorization for a sample to confirm
    # health: the store test's helper moves the authorization back
    _backdate_authorization(controller, session)
    return session


def _healthy_sample(
    observer: ObservationStore, session: UUID, window: tuple[datetime, datetime]
) -> None:
    leases = [
        lease
        for lease in observer.claim_due_samples(uuid4())
        if lease.session_id == session
    ]
    assert len(leases) == 1, leases
    lease = leases[0]
    sample = HealthSample(
        sample_id=str(uuid4()),
        session_id=str(session),
        sequence=lease.sequence,
        window=QueryWindow(start=window[0], end=window[1]),
        outcome="healthy",
        subject_control_generation=lease.subject_control_generation,
        observation_generation=lease.observation_generation,
        health_profile_revision=lease.health_profile_revision,
        required_signals_present=True,
    )
    receipt = observer.submit_sample(
        lease,
        sample,
        [
            SignalReading(
                signal_name="error_ratio",
                status="ok",
                value=0.0,
                sample_count=12,
                query="q",
                window_start=window[0],
                window_end=window[1],
                source="prometheus",
            )
        ],
    )
    assert receipt.accepted and receipt.confirms_health


def _session_row(controller: ObservationStore, session: UUID) -> dict[str, object]:
    with controller.transaction() as conn:
        row = conn.execute(
            "SELECT state, ended_reason, healthy_since, active_sample_job_id, adopted_count FROM opspilot_observation_sessions WHERE session_id=%s",
            (session,),
        ).fetchone()
    return dict(row)


def _endings(controller: ObservationStore, session: UUID) -> list[dict[str, object]]:
    with controller.transaction() as conn:
        rows = conn.execute(
            "SELECT ended_reason, transition, sample_id, lifecycle_before, lifecycle_after FROM opspilot_observation_endings WHERE session_id=%s ORDER BY recorded_at",
            (session,),
        ).fetchall()
    return [dict(row) for row in rows]


def _lifecycle(controller: ObservationStore, incident: UUID) -> str:
    with controller.transaction() as conn:
        row = conn.execute(
            "SELECT lifecycle FROM opspilot_incidents WHERE incident_id=%s",
            (incident,),
        ).fetchone()
    return str(row["lifecycle"])


def test_the_migration_ends_open_sessions_and_a_new_registration_starts_over(
    scratch_dsn: str, observer_dsn: str
) -> None:
    owner = DurableStore(scratch_dsn, pool=POOL)
    owner.install()
    controller = ObservationStore(scratch_dsn, pool=POOL)
    observer = ObservationStore(observer_dsn, pool=POOL)
    try:
        incident, _run, target = _incident(owner)
        open_session = _authorize(controller, incident, target.revision)
        _healthy_sample(observer, open_session, (_now() - timedelta(minutes=5), _now()))
        ended_incident, _run2, ended_target = _incident(owner)
        ended_session = _authorize(controller, ended_incident, ended_target.revision)
        _healthy_sample(
            observer, ended_session, (_now() - timedelta(minutes=5), _now())
        )
        # an ended session is the record of decisions taken (revoked through
        # the store's own primitive before the migration runs)
        assert controller.revoke_sessions(ended_incident) == [ended_session]
        ended_before = _session_row(controller, ended_session)
        ended_endings_before = _endings(controller, ended_session)
        open_before = _session_row(controller, open_session)
        assert open_before["state"] == "authorized"
        assert open_before["healthy_since"] is not None
        assert _lifecycle(controller, incident) == "observing_recovery"

        with psycopg.connect(scratch_dsn) as conn:
            assert end_open_sessions(conn) == 1
            conn.commit()

        # the open session is ended like a pause: revoked, task slot cleared,
        # watermarks kept, one ending record with the lifecycle unchanged
        after = _session_row(controller, open_session)
        assert after["state"] == "revoked"
        assert after["ended_reason"] == "authority_revoked"
        assert after["active_sample_job_id"] is None
        assert after["healthy_since"] == open_before["healthy_since"]
        assert after["adopted_count"] == open_before["adopted_count"]
        assert _endings(controller, open_session) == [
            {
                "ended_reason": "authority_revoked",
                "transition": None,
                "sample_id": None,
                "lifecycle_before": "observing_recovery",
                "lifecycle_after": "observing_recovery",
            }
        ]
        assert _lifecycle(controller, incident) == "observing_recovery"
        # the ended session and its record are untouched
        assert _session_row(controller, ended_session) == ended_before
        assert _endings(controller, ended_session) == ended_endings_before
        # a second run finds nothing to end
        with psycopg.connect(scratch_dsn) as conn:
            assert end_open_sessions(conn) == 0
            conn.commit()
        # the Observer cannot claim the old session any more
        assert [
            lease
            for lease in observer.claim_due_samples(uuid4())
            if lease.session_id == open_session
        ] == []

        # a human registers the remediation again: a new session under the
        # new rule, whose streak starts at its own first healthy window END
        new_session = _authorize(controller, incident, target.revision)
        assert new_session != open_session
        window = (_now() - timedelta(minutes=5), _now())
        _healthy_sample(observer, new_session, window)
        row = _session_row(controller, new_session)
        assert row["state"] == "authorized"
        assert row["healthy_since"] == window[1]
        assert row["adopted_count"] == 1
        # the old session's record is still what the migration left
        assert _session_row(controller, open_session) == after
    finally:
        observer.close()
        controller.close()
        owner.close()
