"""F2 PR-B1 implementer tests on PostgreSQL: incident priority and claim order.

The independent acceptance tests live elsewhere. These cover what the
implementation itself must hold: migration 0019's column, CHECK, grant and
downgrade; the priority an alert or a generic intake writes; and the claim
listing ordered by priority, then deadline (contract U-B1-1/U-B1-2/R-B1-4).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.persistence import DurableStore, PersistenceError
from scripts.m0.postgres_lab import DSN
from tests.integration.test_m1_04_alert_intake_impl_postgres import (
    _alert,
    _build,
    _post,
    _webhook,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

PG_DUMP = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")
VERSIONS = {"tool_schema_revision": "f2-b1-impl"}


def _store() -> DurableStore:
    store = DurableStore(DSN)
    store.install()
    return store


def _priority(incident_id: object) -> int:
    with psycopg.connect(DSN) as conn:
        row = conn.execute(
            "SELECT priority FROM opspilot_incidents WHERE incident_id=%s",
            (incident_id,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _accept(store, *, minutes: float, priority: int | None = None) -> UUID:
    incident, run = uuid4(), uuid4()
    kwargs = {} if priority is None else {"priority": priority}
    store.accept(
        incident,
        run,
        f"f2-b1-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=minutes),
        budget_limit=10,
        versions=VERSIONS,
        **kwargs,
    )
    return incident


def _ours(store, ids) -> list[UUID]:
    wanted = set(ids)
    return [i for i in store.claimable_incidents(limit=100000) if i in wanted]


def test_alert_severity_sets_the_incident_priority_once():
    app, _, store = _build()
    labels = {"alertname": "A", "namespace": "otel-demo", "service": "checkout"}
    cases = {
        "critical": 0,
        " Warning ": 1,
        "info": 2,
        None: 2,
    }
    for severity, expected in cases.items():
        alert_labels = dict(labels)
        if severity is not None:
            alert_labels["severity"] = severity
        alert = _alert(labels=alert_labels)
        [result] = _post(app, _webhook(alert)).json()["results"]
        assert result["outcome"] == "created"
        assert _priority(result["incident_id"]) == expected
        # A replay leaves the stored priority alone.
        [again] = _post(app, _webhook(alert)).json()["results"]
        assert again["outcome"] == "replayed"
        assert _priority(result["incident_id"]) == expected


def test_a_handoff_only_alert_incident_records_its_priority_too():
    app, _, _ = _build()
    alert = _alert(labels={"alertname": "Nowhere", "severity": "critical"})
    [result] = _post(app, _webhook(alert)).json()["results"]
    assert result["outcome"] == "handoff_created" and result["run_id"] is None
    assert _priority(result["incident_id"]) == 0


def test_generic_intake_defaults_to_other_and_invalid_priorities_write_nothing():
    store = _store()
    assert _priority(_accept(store, minutes=10)) == 2
    for bad in (-1, 3, True, "0", 1.0):
        incident, run = uuid4(), uuid4()
        with pytest.raises(PersistenceError, match="^INVALID_INPUT$"):
            store.accept(
                incident,
                run,
                f"f2-b1-bad-{incident}",
                deadline=datetime.now(timezone.utc) + timedelta(minutes=10),
                budget_limit=10,
                versions=VERSIONS,
                priority=bad,  # type: ignore[arg-type]
            )
        with psycopg.connect(DSN) as conn:
            assert not conn.execute(
                "SELECT 1 FROM opspilot_incidents WHERE incident_id=%s", (incident,)
            ).fetchone()


def test_the_column_check_refuses_values_outside_the_domain():
    incident = _accept(_store(), minutes=10, priority=1)
    with psycopg.connect(DSN) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "UPDATE opspilot_incidents SET priority=3 WHERE incident_id=%s",
                (incident,),
            )


def test_claim_listing_orders_by_priority_then_deadline():
    store = _store()
    # (priority, minutes to deadline): deadlines interleave across priorities.
    plan = [(2, 1), (1, 2), (0, 9), (2, 3), (0, 4), (1, 8), (0, 5)]
    made = [(p, m, _accept(store, minutes=m, priority=p)) for p, m in plan]
    # A lapsed ``running`` Run follows the same order as a queued one.
    lapsed = _accept(store, minutes=6, priority=0)
    with psycopg.connect(DSN) as conn:
        conn.execute(
            "UPDATE opspilot_runs SET state='running',owner=%s,lease_until=clock_timestamp()-interval '1 second' "
            "WHERE incident_id=%s",
            (uuid4(), lapsed),
        )
    made.append((0, 6, lapsed))
    expected = [i for _, _, i in sorted(made, key=lambda row: (row[0], row[1]))]
    assert _ours(store, [i for _, _, i in made]) == expected


def test_a_later_critical_incident_lists_ahead_of_earlier_queued_ones():
    store = _store()
    earlier = [_accept(store, minutes=m, priority=1) for m in (1, 2, 3)]
    critical = _accept(store, minutes=30, priority=0)
    assert _ours(store, [*earlier, critical]) == [critical, *earlier]


@pytest.fixture
def scratch_dsn() -> Iterator[str]:
    name = f"opspilot_b1_{uuid4().hex[:12]}"
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


def _column(dsn: str) -> tuple | None:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT data_type,is_nullable,column_default FROM information_schema.columns "
            "WHERE table_name='opspilot_incidents' AND column_name='priority'"
        ).fetchone()


def _web_privileges(dsn: str) -> tuple[bool, bool]:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT has_column_privilege('opspilot_web','opspilot_incidents','priority','INSERT'),"
            "has_column_privilege('opspilot_web','opspilot_incidents','priority','UPDATE')"
        ).fetchone()
    assert row is not None
    return bool(row[0]), bool(row[1])


def test_0019_adds_the_column_with_default_other_and_downgrades_cleanly(
    scratch_dsn: str,
) -> None:
    schema.upgrade_to(scratch_dsn, "0017_web_suspension_grants")
    incident = uuid4()
    with psycopg.connect(scratch_dsn) as conn:
        conn.execute(
            "INSERT INTO opspilot_incidents(incident_id,intake_key,state,lifecycle) VALUES(%s,%s,'queued','open')",
            (incident, f"seed-{incident}"),
        )
    before = schema.schema_dump(scratch_dsn, pg_dump=PG_DUMP)
    schema.upgrade_to(scratch_dsn, "0019_incident_priority")
    assert _column(scratch_dsn) == ("smallint", "NO", "2")
    with psycopg.connect(scratch_dsn) as conn:
        # R-B1-6: no backfill, an existing incident takes the default.
        assert conn.execute(
            "SELECT priority FROM opspilot_incidents WHERE incident_id=%s", (incident,)
        ).fetchone() == (2,)
        has_worker_write = conn.execute(
            "SELECT has_column_privilege('opspilot_worker','opspilot_incidents','priority','INSERT') "
            "OR has_column_privilege('opspilot_worker','opspilot_incidents','priority','UPDATE')"
        ).fetchone()
    assert _web_privileges(scratch_dsn) == (True, True)
    assert has_worker_write == (False,)
    schema.command.downgrade(schema._config(scratch_dsn), "0017_web_suspension_grants")
    assert _column(scratch_dsn) is None
    assert schema.schema_dump(scratch_dsn, pg_dump=PG_DUMP) == before
    schema.upgrade_to(scratch_dsn, "0019_incident_priority")
    assert _web_privileges(scratch_dsn) == (True, True)
