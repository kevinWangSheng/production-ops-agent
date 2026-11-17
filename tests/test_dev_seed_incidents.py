"""The dev seed's fixture shape (web UX spec 2026-11-16, section 7) and its
refusal of non-dev databases. Seeding and ``--reset`` need Postgres."""

from __future__ import annotations

from uuid import UUID

import pytest

from opspilot.web.store import MappingTargetRegistry
from scripts.dev_seed_incidents import (
    MARKER_TITLE,
    NEEDS_HUMAN_KINDS,
    SERVICES,
    TITLE_PREFIX,
    TOTAL,
    alert_for,
    check_dsn,
    plan,
    registry_entries,
    reset_rows,
    service_for,
    title_for,
)


def test_plan_has_exactly_30_waiting_with_the_marker_oldest_at_position_1500():
    kinds = plan()
    assert len(kinds) == TOTAL == 2003
    waiting = [i for i, k in enumerate(kinds) if k in NEEDS_HUMAN_KINDS]
    assert len(waiting) == 30
    marker = kinds.index("marker")
    assert marker == min(waiting) and TOTAL - marker == 1500
    assert MARKER_TITLE in title_for(marker, "marker")
    assert kinds.count("queued") == 12
    assert kinds.count("resolved") == 3
    assert kinds.count("waiting") >= 1
    assert kinds.count("handoff") + kinds.count("ambiguous") >= 1
    assert kinds.count("closed") == TOTAL - 42


def test_titles_are_tagged_long_enough_and_services_are_mixed():
    kinds = plan()
    titles = [title_for(i, k) for i, k in enumerate(kinds)]
    assert all(t.startswith(TITLE_PREFIX) for t in titles)
    assert sum(len(t) > 200 for t in titles) >= 20
    assert len(SERVICES) >= 8
    assert {service_for(i, k) for i, k in enumerate(kinds)} == set(SERVICES)
    assert alert_for(0, "closed")["annotations"]["summary"] == titles[0]


def test_registry_is_valid_and_ambiguous_entries_share_one_match():
    registry = MappingTargetRegistry(registry_entries())
    assert len(registry.match_alert({"service": "ambiguous"})) == 2
    for name in SERVICES:
        assert len(registry.match_alert({"service": name})) == 1
    assert registry.match_alert({"service": "unmapped-1"}) == ()


@pytest.mark.parametrize("name", ["opspilot", "production", "m0_budget"])
def test_refuses_databases_without_dev_or_test_in_the_name(name):
    with pytest.raises(SystemExit):
        check_dsn(f"host=127.0.0.1 dbname={name}", environ={})


@pytest.mark.parametrize("name", ["opspilot_dev", "ux_test"])
def test_accepts_dev_and_test_databases(name):
    assert check_dsn(f"host=127.0.0.1 dbname={name}", environ={}) == name


# -- review fixes (#684 Codex review) ---------------------------------------


@pytest.mark.parametrize(
    "name",
    ["contest", "device_registry", "opspilot_latest", "devops", "testing", "Dev", ""],
)
def test_dev_token_must_be_whole(name):
    with pytest.raises(SystemExit):
        check_dsn(f"host=127.0.0.1 dbname='{name}'", environ={})


@pytest.mark.parametrize(
    "name", ["dev", "test", "seed", "opspilot_dev", "ux_test_1", "dev_list", "a_seed_b"]
)
def test_dev_token_accepted(name):
    assert check_dsn(f"host=localhost dbname={name}", environ={}) == name


@pytest.mark.parametrize(
    "dsn, environ",
    [
        ("host=db.internal dbname=opspilot_dev", {}),
        ("host=10.0.0.5 dbname=opspilot_dev", {}),
        ("host=localhost,db.internal dbname=opspilot_dev", {}),
        ("host=localhost hostaddr=10.0.0.5 dbname=opspilot_dev", {}),
        ("dbname=opspilot_dev", {"PGHOST": "prod.example.com"}),
        ("dbname=opspilot_dev", {"PGHOSTADDR": "10.1.1.1"}),
    ],
)
def test_remote_hosts_are_refused(dsn, environ):
    with pytest.raises(SystemExit):
        check_dsn(dsn, environ=environ)


@pytest.mark.parametrize(
    "dsn",
    [
        "dbname=opspilot_dev",
        "host=/tmp dbname=opspilot_dev",
        "host=127.0.0.1 port=55431 dbname=opspilot_dev",
        "host=::1 dbname=opspilot_dev",
        "postgresql://u@localhost:5432/opspilot_dev",
    ],
)
def test_local_hosts_are_accepted(dsn):
    assert check_dsn(dsn, environ={}) == "opspilot_dev"


class _Result:
    def __init__(self, rows=(), rowcount=0):
        self._rows = list(rows)
        self.rowcount = rowcount

    def fetchall(self):
        return self._rows


class _FakeConn:
    """Answers reset_rows' reads and records every statement."""

    SEED = [UUID("00000000-0000-4000-8000-000000000001")]

    def __init__(self):
        self.statements: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.statements.append((sql, tuple(params)))
        if "FROM opspilot_alert_deliveries" in sql and sql.startswith("SELECT"):
            return _Result([{"incident_id": i} for i in self.SEED])
        if sql.startswith("SELECT intake_key"):
            return _Result([{"intake_key": "intake:seed-1"}])
        if "pg_constraint" in sql:
            return _Result(
                [
                    {
                        "child": "opspilot_incidents",
                        "parent": "opspilot_targets",
                        "child_cols": ["target_id"],
                        "parent_cols": ["target_id"],
                    },
                    {
                        "child": "opspilot_runs",
                        "parent": "opspilot_incidents",
                        "child_cols": ["incident_id"],
                        "parent_cols": ["incident_id"],
                    },
                ]
            )
        if "information_schema" in sql:
            return _Result([{"table_name": "opspilot_runs"}])
        return _Result(rowcount=1)


def test_reset_deletes_only_the_seed_ids_and_nothing_by_pattern():
    conn = _FakeConn()
    assert reset_rows(conn) == 1
    deletes = [(sql, p) for sql, p in conn.statements if sql.startswith("DELETE")]
    assert deletes
    for sql, params in deletes:
        assert "LIKE" not in sql.upper()
        assert all("%" not in str(v) for v in params)
    incidents = [
        d for d in deletes if d[0].startswith("DELETE FROM opspilot_incidents")
    ]
    assert incidents == [
        ("DELETE FROM opspilot_incidents WHERE incident_id = ANY(%s)", (conn.SEED,))
    ]
    # No delete reaches incidents through targets (the P1 bug): the only
    # statement on opspilot_targets keeps a target any row still refers to.
    targets = [d for d in deletes if "FROM opspilot_targets" in d[0]]
    assert len(targets) == 1
    sql, params = targets[0]
    assert (
        "NOT EXISTS (SELECT 1 FROM opspilot_incidents x WHERE x.target_id=t.target_id)"
        in sql
    )
    assert set(params[0]) == set(registry_entries())
    ledger = [d for d in deletes if "opspilot_web_ledger" in d[0]]
    assert len(ledger) == 1
    assert ledger[0][1] == (["intake:seed-1"], [str(conn.SEED[0])], [str(conn.SEED[0])])


def test_reset_with_no_seed_rows_deletes_nothing():
    conn = _FakeConn()
    conn.SEED = []
    assert reset_rows(conn) == 0
    assert not [s for s, _ in conn.statements if s.startswith("DELETE")]


@pytest.mark.parametrize(
    "dsn, environ",
    [
        ("service=prod dbname=opspilot_dev host=localhost", {}),
        ("postgresql://localhost/opspilot_dev?service=prod", {}),
        ("host=localhost dbname=opspilot_dev", {"PGSERVICE": "prod"}),
        (
            "host=localhost dbname=opspilot_dev",
            {"PGSERVICEFILE": "/etc/pg_service.conf"},
        ),
    ],
)
def test_libpq_service_files_are_refused(dsn, environ):
    with pytest.raises(SystemExit):
        check_dsn(dsn, environ=environ)


def test_empty_service_variables_are_not_in_play():
    assert check_dsn("host=localhost dbname=opspilot_dev", environ={"PGSERVICE": ""})
