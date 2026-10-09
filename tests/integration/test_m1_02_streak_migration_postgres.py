"""Migration 0006 (#157) on real PostgreSQL: an open session's healthy streak
mark is cleared, an ended session's record is left alone.

The data step is the migration's own SQL (``RESTART_OPEN_STREAKS_SQL``),
run here on rows the store wrote: the mark exists only after a healthy
sample was adopted, so the test takes one through the Observer login first.
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
recompute_open_streaks = _module.recompute_open_streaks

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


def _session_with_a_healthy_streak(
    owner: DurableStore, controller: ObservationStore, observer: ObservationStore
) -> tuple[UUID, UUID]:
    incident, _run, target = _incident(owner)
    session = controller.authorize_session(
        incident,
        revision=target.revision,
        actor="tester",
        deadline_at=_now() + timedelta(hours=1),
        max_samples=10,
        sample_interval_seconds=15,
        sustained_window_seconds=3600,
        health_profile_revision=PROFILE,
        health_profile=PROFILE_CONTENT,
        first_sample_due_at=_now(),
    )
    # the window must start after the authorization for the sample to
    # confirm health: the store test's helper moves the authorization back
    _backdate_authorization(controller, session)
    leases = [
        lease
        for lease in observer.claim_due_samples(uuid4())
        if lease.session_id == session
    ]
    assert len(leases) == 1
    lease = leases[0]
    window = (_now() - timedelta(minutes=5), _now())
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
    return session, incident


def _mark(controller: ObservationStore, session: UUID) -> tuple[str, datetime | None]:
    with controller.transaction() as conn:
        row = conn.execute(
            "SELECT state, healthy_since FROM opspilot_observation_sessions WHERE session_id=%s",
            (session,),
        ).fetchone()
    return str(row["state"]), row["healthy_since"]


def test_the_migration_restarts_open_streaks_and_leaves_ended_sessions_alone(
    scratch_dsn: str, observer_dsn: str
) -> None:
    owner = DurableStore(scratch_dsn, pool=POOL)
    owner.install()
    controller = ObservationStore(scratch_dsn, pool=POOL)
    observer = ObservationStore(observer_dsn, pool=POOL)
    try:
        open_session, _ = _session_with_a_healthy_streak(owner, controller, observer)
        ended_session, ended_incident = _session_with_a_healthy_streak(
            owner, controller, observer
        )
        # an ended session is the record of decisions taken: the migration
        # must not touch it (revoked through the store's own primitive)
        assert controller.revoke_sessions(ended_incident) == [ended_session]
        state, before = _mark(controller, open_session)
        assert state == "authorized" and before is not None
        _, ended_before = _mark(controller, ended_session)
        assert ended_before is not None

        # the row as the previous code left it: the mark at the window START
        _, written_end = _mark(controller, open_session)
        with controller.transaction() as conn:
            row = conn.execute(
                "SELECT window_start, window_end FROM opspilot_observation_samples WHERE session_id=%s AND disposition='adopted' ORDER BY sequence DESC LIMIT 1",
                (open_session,),
            ).fetchone()
            conn.execute(
                "UPDATE opspilot_observation_sessions SET healthy_since=%s WHERE session_id=%s",
                (row["window_start"], open_session),
            )
        assert written_end == row["window_end"]
        assert _mark(controller, open_session) == ("authorized", row["window_start"])

        with psycopg.connect(scratch_dsn) as conn:
            assert recompute_open_streaks(conn, from_window_end=True) >= 1
            conn.commit()
        # upgrade: the first streak sample's window END; the ended session untouched
        assert _mark(controller, open_session) == ("authorized", row["window_end"])
        assert _mark(controller, ended_session) == ("revoked", ended_before)

        with psycopg.connect(scratch_dsn) as conn:
            recompute_open_streaks(conn, from_window_end=False)
            conn.commit()
        # downgrade: back to the window START (the previous rule)
        assert _mark(controller, open_session) == ("authorized", row["window_start"])

        with psycopg.connect(scratch_dsn) as conn:
            recompute_open_streaks(conn, from_window_end=True)
            conn.commit()
        # and the next healthy sample under the new rule extends the same streak
        # (the next task is due an interval later; bring it forward)
        with controller.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_observation_sessions SET active_sample_due_at=clock_timestamp() - interval '1 minute' WHERE session_id=%s",
                (open_session,),
            )
        leases = [
            lease
            for lease in observer.claim_due_samples(uuid4())
            if lease.session_id == open_session
        ]
        assert len(leases) == 1
        lease = leases[0]
        # contiguous with the previous window (no gap) and not in the future
        window = (row["window_end"], _now())
        sample = HealthSample(
            sample_id=str(uuid4()),
            session_id=str(open_session),
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
        assert _mark(controller, open_session)[1] == row["window_end"]
    finally:
        observer.close()
        controller.close()
        owner.close()
