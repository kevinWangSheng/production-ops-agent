"""#429 `python -m opspilot.schema downgrade --to` against a self-created
Unix-socket PostgreSQL (never the shared lab): output, exit codes, the 0024
outbox counts, refusals that change nothing, and a refusal part-way down.

M1_DURABLE_POSTGRES=1 PGTZ=UTC .venv/bin/python -m pytest \
    tests/test_429_schema_downgrade_postgres_impl.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

PORT = 55479
OLD_HEAD = "0023_retention_raw_cleanup"


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    found = shutil.which("initdb")
    binary_dir = (
        Path(found).parent if found else Path("/opt/homebrew/opt/postgresql@17/bin")
    )
    if os.geteuid() == 0 or any(
        not (binary_dir / name).is_file() for name in ("initdb", "pg_ctl")
    ):
        pytest.skip("self-owned initdb binaries/user unavailable")
    with tempfile.TemporaryDirectory(
        prefix="r429-pg-",
        dir="/private/tmp" if Path("/private/tmp").is_dir() else "/tmp",
    ) as scratch:
        root = Path(scratch)
        data, socket_dir = root / "data", root / "socket"
        socket_dir.mkdir(mode=0o700)
        env = {
            key: value
            for key, value in os.environ.items()
            if key in ("PATH", "HOME", "TMPDIR", "LANG", "TZ") or key.startswith("LC_")
        }

        def run(name: str, *args: object) -> None:
            subprocess.run(
                [str(binary_dir / name), *map(str, args)],
                capture_output=True,
                check=True,
                timeout=60,
                env=env,
            )

        run("initdb", "-D", data, "-U", "opspilot_owner", "--auth=trust",
            "--no-locale", "--encoding=UTF8")  # fmt: skip
        with (data / "postgresql.conf").open("a") as config:
            config.write(
                f"\nlisten_addresses=''\nport={PORT}\nunix_socket_directories='{socket_dir}'\n"
                "shared_buffers=16MB\nmax_connections=20\ntimezone='UTC'\n"
            )
        run("pg_ctl", "-D", data, "-l", root / "postgres.log", "-w", "start")
        try:
            yield make_conninfo(
                host=str(socket_dir), port=PORT, dbname="postgres",
                user="opspilot_owner", connect_timeout=5,
            )  # fmt: skip
        finally:
            run("pg_ctl", "-D", data, "-m", "fast", "-w", "stop")


@pytest.fixture
def database(server: str) -> Iterator[str]:
    name = f"r429_{uuid4().hex[:10]}"
    with psycopg.connect(server, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = make_conninfo(server, dbname=name)
    schema.upgrade_head(dsn)
    yield dsn
    with psycopg.connect(server, autocommit=True) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
        )


def _cli(dsn: str, *argv: str, monkeypatch, capsys) -> tuple[int, str, str]:
    monkeypatch.setenv("OPSPILOT_DSN", dsn)
    code = schema.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def _revision(dsn: str) -> str | None:
    with psycopg.connect(dsn) as conn:
        return schema.current_revision(conn)


def _counts(dsn: str) -> dict[str, int]:
    with psycopg.connect(dsn) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE tablename LIKE 'opspilot\\_%' "
                "ORDER BY 1"
            )
        ]
        return {
            table: conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
            ).fetchone()[0]
            for table in tables
        }


def _incident(
    conn: psycopg.Connection, state: str = "investigating", **extra: object
) -> object:
    incident = uuid4()
    columns = ["incident_id", "state", "control_generation", "intake_key", *extra]
    conn.execute(
        sql.SQL("INSERT INTO opspilot_incidents({}) VALUES ({})").format(
            sql.SQL(",").join(map(sql.Identifier, columns)),
            sql.SQL(",").join(sql.Placeholder() * len(columns)),
        ),
        (incident, state, 0, uuid4().hex, *extra.values()),
    )
    return incident


def test_downgrade_0024_prints_revisions_and_outbox_counts(
    database, monkeypatch, capsys
):
    with psycopg.connect(database) as conn:
        incident = _incident(conn)
        for i, state in enumerate(("pending", "pending", "delivered", "dropped")):
            conn.execute(
                "INSERT INTO opspilot_trace_outbox(idempotency_key,incident_id,kind,"
                "payload,state,finalized_at) VALUES(%s,%s,'generation','{}',%s,"
                "CASE WHEN %s='pending' THEN NULL ELSE clock_timestamp() END)",
                (f"k{i}", incident, state, state),
            )
    before = _counts(database)
    code, out, err = _cli(
        database, "downgrade", "--to", OLD_HEAD, monkeypatch=monkeypatch, capsys=capsys
    )
    assert code == 0, err
    assert out.splitlines() == [
        "schema current: 0024_trace_outbox",
        f"schema target: {OLD_HEAD}",
        f"schema downgraded: {OLD_HEAD}",
    ]
    assert (
        "opspilot_trace_outbox dropped by downgrade: pending=2, delivered=1, dropped=1"
        in err
    )
    assert _revision(database) == OLD_HEAD
    after = _counts(database)
    assert "opspilot_trace_outbox" not in after
    before.pop("opspilot_trace_outbox")
    assert after == before
    # The newer build's runtimes now refuse to start; migrate brings it back.
    with psycopg.connect(database) as conn, pytest.raises(schema.SchemaNotMigrated):
        schema.verify_head(conn)
    assert schema.migrate(database).action == "upgraded"


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("0023", "unknown revision 0023: not a migration in this build"),
        ("base", "unknown revision base: not a migration in this build"),
        (
            "0024_trace_outbox",
            "revision 0024_trace_outbox is not lower than the current",
        ),
    ],
)
def test_refused_targets_change_nothing(database, monkeypatch, capsys, target, message):
    code, out, err = _cli(
        database, "downgrade", "--to", target, monkeypatch=monkeypatch, capsys=capsys
    )
    assert code == 1
    assert message in err
    assert "schema downgraded" not in out and "schema target" not in out
    assert _revision(database) == schema.head_revision()


def test_an_unversioned_database_is_refused(server, monkeypatch, capsys):
    name = f"r429_{uuid4().hex[:10]}"
    with psycopg.connect(server, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        code, _out, err = _cli(
            make_conninfo(server, dbname=name), "downgrade", "--to", OLD_HEAD,
            monkeypatch=monkeypatch, capsys=capsys,
        )  # fmt: skip
        assert code == 1
        assert "schema has no alembic_version table: nothing to downgrade" in err
    finally:
        with psycopg.connect(server, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def test_a_refusing_migration_stops_where_it_is_and_says_so(
    database, monkeypatch, capsys
):
    with psycopg.connect(database) as conn:
        survivor = _incident(conn)
        _incident(conn, state="closed", merged_into_incident_id=survivor)
    before = _counts(database)
    code, out, err = _cli(
        database, "downgrade", "--to", "0021_release_incident_links",
        monkeypatch=monkeypatch, capsys=capsys,
    )  # fmt: skip
    assert code == 1
    assert out.splitlines() == [
        "schema current: 0024_trace_outbox",
        "schema target: 0021_release_incident_links",
    ]
    assert "downgrade refused: 1 merged incident(s)" in err
    # 0024 and 0023 went down in their own transactions; 0022 rolled back.
    assert "schema now at 0022_incident_association" in err
    assert _revision(database) == "0022_incident_association"
    after = _counts(database)
    before.pop("opspilot_trace_outbox")
    assert after == before
