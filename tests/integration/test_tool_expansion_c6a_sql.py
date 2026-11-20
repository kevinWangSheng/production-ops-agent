"""Real-database integration tests for tool expansion C6a (#570), R8-25 and
r11 C6-18 ... C6-21 (C6a part): ``database_sql_query``,
``database_sql_list_tables`` and ``database_sql_describe_table`` against
real PostgreSQL 17.2, MySQL 8.0.40 and MariaDB 11.6.2, through the public
tool path. No model is called.

Runs only in the CI job ``db-backends`` (``OPSPILOT_DB_BACKENDS=1``), which
starts the containers; skipped everywhere else (C6-18).

Written by the independent test author from the C6 task record
(「CI（R8-25）」, eleven assertions ①-⑪) without reading the implementation.
The fixtures, not the product, write: an administrator connection creates
the schema, the synthetic rows and the C6-21 read-only account
(``c6_tests_ro``). The tool only ever gets
the read-only account, except where an assertion is about what happens
when it gets the administrator (⑥, ⑨) or a deliberately over-privileged
account (C6-U1 cases).

CI environment this file assumes (defaults in parentheses); the job sets
them or uses the defaults:

* ``C6_DB_HOST`` (127.0.0.1); ``C6_PG_PORT`` (53201), ``C6_MYSQL_PORT``
  (53202), ``C6_MARIADB_PORT`` (53203); ``C6_UNREACHABLE_PORT`` (53209,
  nothing listens).
* Administrators ``C6_PG_ADMIN_USER`` (``postgres``),
  ``C6_MYSQL_ADMIN_USER`` / ``C6_MARIADB_ADMIN_USER`` (``root``), sharing
  ``C6_ADMIN_PASSWORD`` (``changeme570``). Low-entropy CI-only values.
* ``C6_PG_CONTAINER`` (``c6-postgres``): the PostgreSQL container name,
  started with ``-c log_statement=all``; ``docker logs`` of it is the
  statement log for ⑨. MySQL and MariaDB run with
  ``--general-log=1 --log-output=TABLE`` (read from ``mysql.general_log``).
* This file creates its own database ``c6_tests``, read-only account
  ``c6_tests_ro`` (C6-21 grants), the stand-in OpsPilot database
  ``c6_tests_opspilot`` (with ``opspilot_runs``) and ``c6_tests_*`` test
  accounts; the implementer's file uses ``c6_impl*``. It runs first.
* PostgreSQL and MariaDB are reached with ``OPSPILOT_SQL_TLS=disable``
  (no server certificate for PostgreSQL; MariaDB accounts use
  ``mysql_native_password``); MySQL 8 with ``require`` (its auto-generated
  self-signed certificate, ⑪): caching_sha2_password over plaintext would
  need the ``cryptography`` package, which the project does not install.
  Engineer-side MySQL 8 connections pass an unverified SSLContext.
* ``jq`` and ``python3`` on PATH (the extension worker's preconditions).
"""

from __future__ import annotations

import itertools
import os
import re
import shutil
import ssl
import subprocess
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest

from opspilot.tools import ToolRequest
from opspilot.tools.outcomes import Window
from opspilot.tools.profiles import PROFILE_ENV, ToolProfile, select_profile
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import NOW, TARGET_ID, FakeStore, _lease

pytestmark = pytest.mark.skipif(
    os.environ.get("OPSPILOT_DB_BACKENDS") != "1",
    reason="real databases run only in the CI db-backends job",
)

QUERY = "database_sql_query"
LIST_TABLES = "database_sql_list_tables"
DESCRIBE = "database_sql_describe_table"

HOST = os.environ.get("C6_DB_HOST", "127.0.0.1")
PG_PORT = int(os.environ.get("C6_PG_PORT", "53201"))
MYSQL_PORT = int(os.environ.get("C6_MYSQL_PORT", "53202"))
MARIADB_PORT = int(os.environ.get("C6_MARIADB_PORT", "53203"))
UNREACHABLE_PORT = int(os.environ.get("C6_UNREACHABLE_PORT", "53209"))
PG_ADMIN = os.environ.get("C6_PG_ADMIN_USER", "postgres")
MY_ADMIN = {
    "mysql": os.environ.get("C6_MYSQL_ADMIN_USER", "root"),
    "mariadb": os.environ.get("C6_MARIADB_ADMIN_USER", "root"),
}
ADMIN_PASSWORD = os.environ.get("C6_ADMIN_PASSWORD", "changeme570")
PG_ADMIN_PASSWORD = ADMIN_PASSWORD
MYSQL_ROOT_PASSWORD = ADMIN_PASSWORD
PG_CONTAINER = os.environ.get("C6_PG_CONTAINER", "c6-postgres")

DB = "c6_tests"
OPSMOCK_DB = "c6_tests_opspilot"
OWNED_DB = "c6_tests_owned"
RO = "c6_tests_ro"
RO_PASSWORD = "ro-" + "pw-570"  # low entropy, CI only
CANARY = "aaaaaaaa570"
BULK_ROWS = 600
LONG_CHARS = 5000
BIG_ROWS, BIG_CHARS = 500, 4500  # 2.25 MB before the 2000-char cut
WORDY_ROWS = 500

BACKENDS = ("postgresql", "mysql", "mariadb")
DIALECT = {"postgresql": "postgresql", "mysql": "mysql", "mariadb": "mysql"}
PORT = {"postgresql": PG_PORT, "mysql": MYSQL_PORT, "mariadb": MARIADB_PORT}
SCHEME = {"postgresql": "postgresql", "mysql": "mysql", "mariadb": "mariadb"}
TLS = {"postgresql": "disable", "mysql": "require", "mariadb": "disable"}

FRAME = Window(NOW - timedelta(hours=6), NOW)
_INDEX = itertools.count()


def _marker(label: str) -> str:
    return f"c6t-{label}-{uuid.uuid4().hex[:12]}"


# -- administrator connections (engineer side, never the product) -------------


def _pg(
    dbname: str = "postgres", user: str = PG_ADMIN, password: str = PG_ADMIN_PASSWORD
):
    import psycopg

    return psycopg.connect(
        host=HOST,
        port=PG_PORT,
        dbname=dbname,
        user=user,
        password=password,
        autocommit=True,
        connect_timeout=10,
    )


def _insecure_tls() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _my(
    backend: str,
    user: str | None = None,
    password: str = MYSQL_ROOT_PASSWORD,
    database: str | None = None,
):
    import pymysql

    kwargs: dict[str, Any] = {
        "host": HOST,
        "port": PORT[backend],
        "user": user or MY_ADMIN[backend],
        "password": password,
        "autocommit": True,
        "connect_timeout": 10,
    }
    if database:
        kwargs["database"] = database
    if backend == "mysql":
        # caching_sha2_password needs TLS (or RSA) for the first login.
        kwargs["ssl"] = _insecure_tls()
    return pymysql.connect(**kwargs)


def _admin_scalar(
    backend: str, sql: str, params: tuple = (), database: str = DB
) -> Any:
    if backend == "postgresql":
        with _pg(database) as conn:
            row = conn.execute(sql, params).fetchone()
            return row[0] if row else None
    conn = _my(backend, database=database)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def _count(backend: str, table: str) -> int:
    return int(_admin_scalar(backend, f"SELECT count(*) FROM {table}"))


def _tables(backend: str) -> list[str]:
    if backend == "postgresql":
        with _pg(DB) as conn:
            rows = conn.execute(
                "SELECT table_schema || '.' || table_name FROM information_schema.tables "
                "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY 1"
            ).fetchall()
    else:
        conn = _my(backend)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT CONCAT(table_schema, '.', table_name) FROM "
                    "information_schema.tables WHERE table_schema = %s ORDER BY 1",
                    (DB,),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
    return [r[0] for r in rows]


def _words(n: int) -> str:
    return " ".join(f"word{n}-{k} checkout latency spike" for k in range(55))


# -- fixtures: schema, data and accounts (C6-21) ---------------------------------

#: PostgreSQL accounts that must be refused by the account check (C6-U1):
#: name suffix -> statements run by the administrator after the role exists
#: with LOGIN, CONNECT on c6_tests and pg_read_all_data.
PG_BAD_ROLES: dict[str, list[str]] = {
    "super": ["ALTER ROLE {r} SUPERUSER"],
    "createrole": ["ALTER ROLE {r} CREATEROLE"],
    "createdb": ["ALTER ROLE {r} CREATEDB"],
    "replication": ["ALTER ROLE {r} REPLICATION"],
    "bypassrls": ["ALTER ROLE {r} BYPASSRLS"],
    "write_all": ["GRANT pg_write_all_data TO {r}"],
    "signal": ["GRANT pg_signal_backend TO {r}"],
    "insert": ["GRANT INSERT ON c6t_orders TO {r}"],
    "update": ["GRANT UPDATE (note) ON c6t_orders TO {r}"],
    "delete": ["GRANT DELETE ON c6t_orders TO {r}"],
    "truncate": ["GRANT TRUNCATE ON c6t_orders TO {r}"],
    "trigger": ["GRANT TRIGGER ON c6t_orders TO {r}"],
    "references": ["GRANT REFERENCES ON c6t_customers TO {r}"],
    "maintain": ["GRANT MAINTAIN ON c6t_orders TO {r}"],
    "view_write": ["GRANT INSERT ON c6t_order_totals TO {r}"],
    "seq_usage": ["GRANT USAGE ON SEQUENCE c6t_customers_id_seq TO {r}"],
    "seq_update": ["GRANT UPDATE ON SEQUENCE c6t_customers_id_seq TO {r}"],
    "schema_create": ["GRANT CREATE ON SCHEMA public TO {r}"],
    "db_create": ["GRANT CREATE ON DATABASE c6_tests TO {r}"],
    "inherits_writer": ["GRANT c6_tests_writer TO {r}"],
    "can_set_super": ["GRANT c6_tests_superish TO {r} WITH INHERIT FALSE"],
}

#: MySQL/MariaDB accounts the account check must refuse: suffix -> grants.
MY_BAD_USERS: dict[str, list[str]] = {
    "insert": ["GRANT SELECT, INSERT ON c6_tests.* TO {u}"],
    "all_db": ["GRANT ALL PRIVILEGES ON c6_tests.* TO {u}"],
    "grant_option": ["GRANT SELECT ON c6_tests.* TO {u} WITH GRANT OPTION"],
    "temp_tables": ["GRANT SELECT, CREATE TEMPORARY TABLES ON c6_tests.* TO {u}"],
    "lock_tables": ["GRANT SELECT, LOCK TABLES ON c6_tests.* TO {u}"],
    "execute": ["GRANT SELECT, EXECUTE ON c6_tests.* TO {u}"],
    "column_update": ["GRANT SELECT, UPDATE (note) ON c6_tests.c6t_orders TO {u}"],
    "file": ["GRANT SELECT ON c6_tests.* TO {u}", "GRANT FILE ON *.* TO {u}"],
    "role": ["GRANT SELECT ON c6_tests.* TO {u}", "GRANT c6_tests_writer TO {u}"],
    "proxy": [
        "GRANT SELECT ON c6_tests.* TO {u}",
        "GRANT PROXY ON 'c6_tests_ro'@'%' TO {u}",
    ],
}


def _pg_setup() -> None:
    bad = [f"c6_tests_bad_{name}" for name in PG_BAD_ROLES]
    roles = [
        RO,
        "c6_tests_ro_narrow",
        "c6_tests_ro_monitor",
        *bad,
        "c6_tests_writer",
        "c6_tests_superish",
    ]
    with _pg() as conn:
        for db in (DB, OPSMOCK_DB, OWNED_DB):
            conn.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        for role in (*roles, "c6_tests_owner"):
            conn.execute(f"DROP ROLE IF EXISTS {role}")
        conn.execute(f"CREATE DATABASE {DB}")
        conn.execute(f"CREATE DATABASE {OPSMOCK_DB}")
        for role in (RO, "c6_tests_ro_narrow", "c6_tests_ro_monitor", *bad):
            conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{RO_PASSWORD}'")
        conn.execute("CREATE ROLE c6_tests_writer NOLOGIN")
        conn.execute("CREATE ROLE c6_tests_superish NOLOGIN SUPERUSER")
    with _pg(DB) as conn:
        conn.execute(
            """
            CREATE TABLE c6t_customers (
                id serial PRIMARY KEY,
                name text NOT NULL,
                email text UNIQUE,
                password text,
                api_token text,
                created_at timestamptz NOT NULL DEFAULT now()
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE c6t_orders (
                id serial PRIMARY KEY,
                customer_id int NOT NULL REFERENCES c6t_customers (id),
                amount numeric(10, 2) NOT NULL,
                status text DEFAULT 'new',
                note text
            );
            """
        )
        conn.execute("CREATE INDEX c6t_orders_customer_idx ON c6t_orders (customer_id)")
        conn.execute(
            "CREATE VIEW c6t_order_totals AS SELECT customer_id, sum(amount) AS total "
            "FROM c6t_orders GROUP BY customer_id"
        )
        conn.execute("CREATE TABLE c6t_bulk (n int PRIMARY KEY, label text)")
        conn.execute(
            f"INSERT INTO c6t_bulk SELECT g, 'row-' || g FROM generate_series(1, {BULK_ROWS}) g"
        )
        conn.execute("CREATE TABLE c6t_long (id int PRIMARY KEY, body text)")
        conn.execute(f"INSERT INTO c6t_long VALUES (1, repeat('L', {LONG_CHARS}))")
        conn.execute("CREATE TABLE c6t_big (id int PRIMARY KEY, body text)")
        conn.execute(
            f"INSERT INTO c6t_big SELECT g, repeat('b', {BIG_CHARS}) "
            f"FROM generate_series(1, {BIG_ROWS}) g"
        )
        conn.execute("CREATE TABLE c6t_wordy (id int PRIMARY KEY, note text)")
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO c6t_wordy VALUES (%s, %s)",
                [(n, _words(n)) for n in range(WORDY_ROWS)],
            )
            cur.executemany(
                "INSERT INTO c6t_customers (name, email, password, api_token) "
                "VALUES (%s, %s, %s, %s)",
                [
                    (
                        f"customer-{n}",
                        f"c{n}@example.invalid",
                        f"pw-{CANARY}",
                        f"tok-{CANARY}",
                    )
                    for n in range(1, 4)
                ],
            )
            cur.executemany(
                "INSERT INTO c6t_orders (customer_id, amount, status, note) "
                "VALUES (%s, %s, %s, %s)",
                [(1 + n % 3, 10 + n, "paid", f"order-{n}") for n in range(5)],
            )
        conn.execute(
            f"GRANT CONNECT ON DATABASE {DB} TO {RO}, c6_tests_ro_monitor, c6_tests_ro_narrow"
        )
        conn.execute(f"GRANT pg_read_all_data, pg_read_all_stats TO {RO}")
        conn.execute(f"ALTER ROLE {RO} SET default_transaction_read_only = on")
        conn.execute("GRANT pg_read_all_data, pg_monitor TO c6_tests_ro_monitor")
        conn.execute("GRANT SELECT ON c6t_orders TO c6_tests_ro_narrow")
        conn.execute("GRANT INSERT ON c6t_orders TO c6_tests_writer")
        for name, statements in PG_BAD_ROLES.items():
            role = f"c6_tests_bad_{name}"
            conn.execute(f"GRANT CONNECT ON DATABASE {DB} TO {role}")
            conn.execute(f"GRANT pg_read_all_data TO {role}")
            for statement in statements:
                conn.execute(statement.format(r=role))
    with _pg() as conn:
        # A database owned by an otherwise read-only role.
        conn.execute(f"GRANT CONNECT ON DATABASE {OPSMOCK_DB} TO {RO}")
        conn.execute(f"CREATE ROLE c6_tests_owner LOGIN PASSWORD '{RO_PASSWORD}'")
        conn.execute("GRANT pg_read_all_data TO c6_tests_owner")
        conn.execute(f"CREATE DATABASE {OWNED_DB} OWNER c6_tests_owner")
    with _pg(OPSMOCK_DB) as conn:
        # Stands in for OpsPilot's own database (C6-8 ③).
        conn.execute("CREATE TABLE opspilot_runs (id int PRIMARY KEY)")


def _my_setup(backend: str) -> None:
    users = [RO, *(f"c6_tests_bad_{name}" for name in MY_BAD_USERS)]
    conn = _my(backend)
    try:
        with conn.cursor() as cur:
            for user in users:
                cur.execute(f"DROP USER IF EXISTS '{user}'@'%'")
            cur.execute("DROP ROLE IF EXISTS c6_tests_writer")
            cur.execute(f"DROP DATABASE IF EXISTS {DB}")
            cur.execute(f"CREATE DATABASE {DB}")
            cur.execute(f"USE {DB}")
            cur.execute(
                """
                CREATE TABLE c6t_customers (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(100) NOT NULL,
                    email VARCHAR(200) UNIQUE,
                    password VARCHAR(200),
                    api_token VARCHAR(200),
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB
                """
            )
            cur.execute(
                """
                CREATE TABLE c6t_orders (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    customer_id INT NOT NULL,
                    amount DECIMAL(10, 2) NOT NULL,
                    status VARCHAR(20) DEFAULT 'new',
                    note VARCHAR(200),
                    INDEX c6t_orders_customer_idx (customer_id),
                    CONSTRAINT c6t_orders_customer_fk FOREIGN KEY (customer_id)
                        REFERENCES c6t_customers (id)
                ) ENGINE=InnoDB
                """
            )
            cur.execute(
                "CREATE VIEW c6t_order_totals AS SELECT customer_id, SUM(amount) AS total "
                "FROM c6t_orders GROUP BY customer_id"
            )
            cur.execute(
                "CREATE TABLE c6t_bulk (n INT PRIMARY KEY, label VARCHAR(40)) ENGINE=InnoDB"
            )
            cur.executemany(
                "INSERT INTO c6t_bulk VALUES (%s, %s)",
                [(n, f"row-{n}") for n in range(1, BULK_ROWS + 1)],
            )
            cur.execute(
                "CREATE TABLE c6t_long (id INT PRIMARY KEY, body TEXT) ENGINE=InnoDB"
            )
            cur.execute("INSERT INTO c6t_long VALUES (1, %s)", ("L" * LONG_CHARS,))
            cur.execute(
                "CREATE TABLE c6t_big (id INT PRIMARY KEY, body TEXT) ENGINE=InnoDB"
            )
            cur.executemany(
                "INSERT INTO c6t_big VALUES (%s, %s)",
                [(n, "b" * BIG_CHARS) for n in range(1, BIG_ROWS + 1)],
            )
            cur.execute(
                "CREATE TABLE c6t_wordy (id INT PRIMARY KEY, note TEXT) ENGINE=InnoDB"
            )
            cur.executemany(
                "INSERT INTO c6t_wordy VALUES (%s, %s)",
                [(n, _words(n)) for n in range(WORDY_ROWS)],
            )
            cur.executemany(
                "INSERT INTO c6t_customers (name, email, password, api_token) "
                "VALUES (%s, %s, %s, %s)",
                [
                    (
                        f"customer-{n}",
                        f"c{n}@example.invalid",
                        f"pw-{CANARY}",
                        f"tok-{CANARY}",
                    )
                    for n in range(1, 4)
                ],
            )
            cur.executemany(
                "INSERT INTO c6t_orders (customer_id, amount, status, note) "
                "VALUES (%s, %s, %s, %s)",
                [(1 + n % 3, 10 + n, "paid", f"order-{n}") for n in range(5)],
            )
            for user in users:
                cur.execute(
                    f"CREATE USER '{user}'@'%' IDENTIFIED BY %s", (RO_PASSWORD,)
                )
            # C6-21 read-only account.
            cur.execute(f"GRANT SELECT, SHOW VIEW ON {DB}.* TO '{RO}'@'%'")
            cur.execute(f"GRANT SELECT ON performance_schema.* TO '{RO}'@'%'")
            cur.execute(f"GRANT PROCESS ON *.* TO '{RO}'@'%'")
            cur.execute("CREATE ROLE c6_tests_writer")
            cur.execute(f"GRANT INSERT ON {DB}.* TO c6_tests_writer")
            for name, grants in MY_BAD_USERS.items():
                user = f"'c6_tests_bad_{name}'@'%'"
                for grant in grants:
                    cur.execute(grant.format(u=user))
    finally:
        conn.close()


_READY: set[str] = set()


def _ensure(name: str) -> str:
    """Create the schema, rows and accounts once per backend per session."""
    if name not in _READY:
        if name == "postgresql":
            _pg_setup()
        else:
            _my_setup(name)
        _READY.add(name)
    return name


@pytest.fixture(params=BACKENDS)
def backend(request) -> str:
    return _ensure(request.param)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    for name in list(os.environ):
        if name.startswith("OPSPILOT_SQL") or re.fullmatch(
            r"OPSPILOT_[A-Z0-9_]*DSN", name
        ):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="module")
def k8s_ca(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("c6a-k8s") / "synthetic-ca.pem"
    path.write_text("synthetic ca placeholder\n")
    return str(path)


# -- the product path -----------------------------------------------------------


def _url(backend: str, database: str = DB, port: int | None = None) -> str:
    return f"{SCHEME[backend]}://{HOST}:{port or PORT[backend]}/{database}"


def _env(
    backend: str,
    k8s_ca: str,
    *,
    user: str = RO,
    password: str = RO_PASSWORD,
    database: str = DB,
    **extra: str,
) -> dict[str, str]:
    values = {
        PROFILE_ENV: "otel-demo-ext",
        "OPSPILOT_K8S_API_URL": "https://127.0.0.1:16443",
        "OPSPILOT_K8S_TOKEN": "dddddddd570",
        "OPSPILOT_K8S_CA_FILE": k8s_ca,
        "OPSPILOT_K8S_NAMESPACE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        "OPSPILOT_SQL_URL": _url(backend, database),
        "OPSPILOT_SQL_USERNAME": user,
        "OPSPILOT_SQL_PASSWORD": password,
        "OPSPILOT_SQL_TLS": TLS[backend],
    }
    values.update(extra)
    return {k: v for k, v in values.items() if v != ""}


def _executor(env: Mapping[str, str]):
    if shutil.which("jq") is None or shutil.which("python3") is None:
        pytest.fail("the db-backends job must provide jq and python3")
    clock = FakeClock(start=NOW)
    profile = select_profile(env)
    assert isinstance(profile, ToolProfile)
    lease = _lease()
    deadline = NOW + timedelta(minutes=10)
    factory = profile.executor_factory(
        FakeStore(lease, deadline=deadline), RecordingSink(), clock
    )
    return factory(
        lease,
        profile.face(clock).input_for(
            run_id=str(lease.run_id),
            question="Why is checkout degraded?",
            target_id=TARGET_ID,
            deadline=deadline,
            model_requests=12,
        ),
    )


@dataclass
class Tool:
    env: dict[str, str]

    def run(self, tool: str, params: dict[str, object]):
        return _executor(self.env).execute(
            ToolRequest(
                step_id="c6a-live",
                tool_index=next(_INDEX),
                tool_name=tool,
                target_ref=TARGET_ID,
                params=params,
                window=FRAME.as_json(),
            )
        )

    def query(self, sql: str, **params: object):
        return self.run(QUERY, {"sql": sql, **params})


@pytest.fixture
def ro(backend, k8s_ca) -> Tool:
    return Tool(_env(backend, k8s_ca))


def _view(outcome) -> Mapping[str, Any]:
    assert outcome.model_view is not None
    return outcome.model_view


def _rows(outcome) -> list[Mapping[str, Any]]:
    content = _view(outcome)["content"]
    assert isinstance(content, list), _view(outcome)
    return content


def _notes(outcome) -> list[str]:
    return [str(n) for n in (_view(outcome).get("notes") or [])]


def _assert(outcome, status: str, reason: str | None, first_note: str | None = None):
    assert (outcome.status, outcome.reason) == (status, reason), _view(outcome)
    if first_note is not None:
        assert (_notes(outcome) or [""])[0].startswith(first_note), _view(outcome)


def _admin_env(backend: str, k8s_ca: str, **extra: str) -> dict[str, str]:
    if backend == "postgresql":
        return _env(backend, k8s_ca, user=PG_ADMIN, password=PG_ADMIN_PASSWORD, **extra)
    return _env(
        backend, k8s_ca, user=MY_ADMIN[backend], password=MYSQL_ROOT_PASSWORD, **extra
    )


# == ① real data from all three tools =============================================


def test_query_returns_real_rows_as_column_value_maps(backend, ro):
    outcome = ro.query("SELECT id, name, email FROM c6t_customers ORDER BY id")
    _assert(outcome, "ok", None)
    rows = _rows(outcome)
    assert [r["name"] for r in rows] == ["customer-1", "customer-2", "customer-3"]
    assert [k for k in rows[0] if k != "_truncated_fields"] == ["id", "name", "email"]
    view = _view(outcome)
    assert view["dialect"] == DIALECT[backend]
    assert view["incomplete"] is False
    assert view["source_start_at"] is None and view["source_end_at"] is None


def test_list_tables_returns_tables_and_views_sorted(backend, ro):
    schema = "public" if backend == "postgresql" else DB
    outcome = ro.run(LIST_TABLES, {})
    _assert(outcome, "ok", None)
    rows = [r for r in _rows(outcome) if str(r["name"]).startswith("c6t_")]
    by_name = {r["name"]: r for r in rows}
    assert by_name["c6t_customers"] == {
        "schema": schema,
        "name": "c6t_customers",
        "type": "table",
    }
    assert by_name["c6t_order_totals"]["type"] == "view"
    assert {"c6t_orders", "c6t_bulk", "c6t_long", "c6t_big", "c6t_wordy"} <= set(
        by_name
    )
    everything = _rows(outcome)
    keys = [(str(r["schema"]), str(r["name"])) for r in everything]
    assert keys == sorted(keys)
    for row in everything:
        assert row["type"] in {
            "table", "view", "materialized_view", "partitioned_table", "foreign_table",
        }  # fmt: skip
        if backend != "postgresql":
            assert row["type"] in {"table", "view"}
    if backend == "postgresql":
        assert not {r["schema"] for r in everything} & {
            "pg_catalog",
            "information_schema",
        }
    no_views = ro.run(LIST_TABLES, {"schema": schema, "include_views": False})
    _assert(no_views, "ok", None)
    names = {r["name"] for r in _rows(no_views)}
    assert "c6t_customers" in names and "c6t_order_totals" not in names


def test_describe_table_returns_columns_keys_and_indexes(backend, ro):
    outcome = ro.run(DESCRIBE, {"table_name": "c6t_orders"})
    _assert(outcome, "ok", None)
    columns = _rows(outcome)
    assert [c["name"] for c in columns] == [
        "id",
        "customer_id",
        "amount",
        "status",
        "note",
    ]
    assert [c["ordinal"] for c in columns] == sorted(c["ordinal"] for c in columns)
    by_name = {c["name"]: c for c in columns}
    assert by_name["customer_id"]["nullable"] is False
    assert by_name["note"]["nullable"] is True
    assert "new" in str(by_name["status"]["default"])
    for column in columns:
        assert set(column) >= {"name", "type", "nullable", "default", "ordinal"}
    view = _view(outcome)
    assert view["primary_key"] == ["id"]
    [fk] = view["foreign_keys"]
    assert fk["columns"] == ["customer_id"]
    assert fk["referred_table"] == "c6t_customers"
    assert fk["referred_columns"] == ["id"]
    indexes = {i["name"]: i for i in view["indexes"]}
    index = indexes["c6t_orders_customer_idx"]
    assert index["columns"] == ["customer_id"] and index["unique"] is False
    if backend == "postgresql":
        assert "CREATE INDEX" in str(index["definition"]).upper()
    else:
        assert index["definition"] is None
    assert any(i["primary"] for i in view["indexes"])
    # schema.table and an explicit schema find the same table.
    schema = "public" if backend == "postgresql" else DB
    dotted = ro.run(DESCRIBE, {"table_name": f"{schema}.c6t_orders"})
    explicit = ro.run(DESCRIBE, {"table_name": "c6t_orders", "schema": schema})
    for other in (dotted, explicit):
        _assert(other, "ok", None)
        assert [c["name"] for c in _rows(other)] == [c["name"] for c in columns]
    view_columns = ro.run(DESCRIBE, {"table_name": "c6t_order_totals"})
    _assert(view_columns, "ok", None)
    assert [c["name"] for c in _rows(view_columns)] == ["customer_id", "total"]
    missing = ro.run(DESCRIBE, {"table_name": "c6t_nope"})
    _assert(missing, "error", "INVALID_PARAMS", "TABLE_NOT_FOUND")


# == ② EXPLAIN ======================================================================


def test_explain_returns_a_plan(backend, ro):
    outcome = ro.query("EXPLAIN SELECT * FROM c6t_orders WHERE customer_id = 1")
    _assert(outcome, "ok", None)
    assert _rows(outcome)
    if backend == "mariadb":
        return  # MariaDB spells it ANALYZE SELECT, which the check refuses.
    analyzed = ro.query(
        "EXPLAIN ANALYZE SELECT * FROM c6t_orders WHERE customer_id = 1"
    )
    _assert(analyzed, "ok", None)
    text = repr(_rows(analyzed)).lower()
    assert "actual" in text, text[:500]


# == ③ limit, long values, size limits ===============================================


def test_limit_truncates_and_marks_incomplete(backend, ro):
    outcome = ro.query("SELECT n, label FROM c6t_bulk ORDER BY n", limit=500)
    _assert(outcome, "ok", None)
    assert [r["n"] for r in _rows(outcome)] == list(range(1, 501))
    view = _view(outcome)
    assert (
        view["incomplete"] is True and "limit" in str(view["incomplete_reason"]).lower()
    )
    small = ro.query("SELECT n FROM c6t_bulk ORDER BY n", limit=10)
    assert [r["n"] for r in _rows(small)] == list(range(1, 11))
    whole = ro.query("SELECT n FROM c6t_bulk WHERE n <= 7 ORDER BY n", limit=7)
    assert _view(whole)["incomplete"] is False


def test_long_values_are_cut(backend, ro):
    outcome = ro.query("SELECT id, body FROM c6t_long")
    _assert(outcome, "ok", None)
    [row] = _rows(outcome)
    assert row["body"].startswith("L" * 1000) and len(row["body"]) < LONG_CHARS
    assert "L" * 2001 not in repr(_view(outcome))
    assert row["_truncated_fields"] == ["body"]
    assert _view(outcome)["long_values_cut"]


def test_raw_rows_over_2_mib_and_oversized_views_are_refused_whole(backend, ro):
    big = ro.query("SELECT id, body FROM c6t_big ORDER BY id", limit=500)
    _assert(big, "error", "RESULT_TOO_LARGE")
    assert _view(big)["content"] is None
    wordy = ro.query("SELECT id, note FROM c6t_wordy ORDER BY id", limit=500)
    _assert(wordy, "error", "RESULT_TOO_LARGE")
    assert _view(wordy)["content"] is None


# == ④ redaction by column name ======================================================


def test_password_and_token_columns_are_redacted(backend, ro, record_property):
    outcome = ro.query("SELECT id, password, api_token FROM c6t_customers ORDER BY id")
    _assert(outcome, "ok", None)
    assert CANARY not in repr(_view(outcome))
    assert "REDACTED" in repr(_view(outcome))
    # Residual risk (PRODUCT-CONSTRAINTS:40): an alias hides the column name.
    # Recorded as evidence, not asserted.
    aliased = ro.query("SELECT password AS p, api_token AS t FROM c6t_customers")
    leaked = CANARY in repr(_view(aliased))
    record_property(
        f"c6a_alias_redaction_{backend}", "leaked" if leaked else "redacted"
    )
    print(
        f"C6A-EVIDENCE alias redaction on {backend}: {'leaked' if leaked else 'redacted'}"
    )


# == ⑤ the statement check on a real database ========================================

REFUSED_ON_REAL = {
    "postgresql": [
        "DELETE FROM c6t_orders",
        "WITH x AS (DELETE FROM c6t_orders RETURNING *) SELECT count(*) FROM x",
        "SELECT 1; DELETE FROM c6t_orders",
        "SELECT * INTO c6t_copy FROM c6t_orders",
        "SELECT * FROM c6t_orders FOR UPDATE",
        "SELECT pg_sleep(1)",
        "SELECT lo_import('/etc/hostname')",
        r"SELECT 'a\' ; DELETE FROM c6t_orders; -- '",
        "CREATE TABLE c6t_new (a int)",
        "SELECT $$ x $$; DROP TABLE c6t_orders",
    ],
    "mysql": [
        "DELETE FROM c6t_orders",
        "SELECT 1; DELETE FROM c6t_orders",
        "SELECT * FROM c6t_orders FOR UPDATE",
        "SELECT * FROM c6t_orders INTO OUTFILE '/tmp/c6t.out'",
        "SELECT SLEEP(1)",
        "SELECT 1 /*!50000 , (SELECT COUNT(*) FROM c6t_orders) */",
        r"SELECT 'a\' ; DELETE FROM c6t_orders; -- '",
        "SELECT 1 --1",
        "CREATE TABLE c6t_new (a int)",
        "SELECT 1 # x\n; DROP TABLE c6t_orders",
    ],
}


def test_refused_statements_change_nothing_on_the_real_database(backend, ro):
    before_rows = _count(backend, "c6t_orders")
    before_tables = _tables(backend)
    for sql in REFUSED_ON_REAL[DIALECT[backend]]:
        outcome = ro.query(sql)
        assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), (
            sql,
            _view(outcome),
        )
        first = (_notes(outcome) or [""])[0]
        assert first.startswith(("QUERY_CONSTRUCT_NOT_ALLOWED", "INVALID_QUERY")), (
            sql,
            first,
        )
    assert _count(backend, "c6t_orders") == before_rows
    assert _tables(backend) == before_tables


# == ⑥ the session read-only layer holds with the check skipped ======================


def test_read_only_transaction_refuses_an_administrator_insert(
    backend, k8s_ca, monkeypatch
):
    # ASSUMPTION: the transport looks both functions up on the module at call
    # time (the record names them as the seams this test patches).
    from opspilot.tools import sql_query

    monkeypatch.setattr(sql_query, "sql_statement_problem", lambda sql, dialect: None)
    monkeypatch.setattr(sql_query, "session_problem", lambda dialect, rows: None)
    admin = Tool(_admin_env(backend, k8s_ca))
    before = _count(backend, "c6t_orders")
    outcome = admin.query(
        "INSERT INTO c6t_orders (customer_id, amount, status, note) VALUES (1, 1, 'x', 'c6t-ro-txn')"
    )
    # Only the database can answer SESSION_READ_ONLY: the INSERT was sent and
    # the read-only transaction refused it.
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_FORBIDDEN"), _view(
        outcome
    )
    assert "SESSION_READ_ONLY" in " ".join(_notes(outcome)), _view(outcome)
    assert _count(backend, "c6t_orders") == before


# == ⑦ the identity layer: the read-only account itself cannot write ================

DIRECT_WRITES = [
    "INSERT INTO c6t_orders (customer_id, amount, status, note) VALUES (1, 1, 'x', 'c6t-direct')",
    "UPDATE c6t_orders SET amount = 0",
    "DELETE FROM c6t_orders",
    "CREATE TABLE c6t_direct (a int)",
    "DROP TABLE c6t_orders",
]


def test_read_only_account_cannot_write_when_connected_directly(backend):
    before_rows = _count(backend, "c6t_orders")
    before_tables = _tables(backend)
    if backend == "postgresql":
        import psycopg

        with _pg(DB, RO, RO_PASSWORD) as conn:
            for sql in DIRECT_WRITES:
                # Leave default_transaction_read_only aside: this checks the
                # privileges, not the session setting.
                conn.execute("BEGIN READ WRITE")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(sql)
                conn.execute("ROLLBACK")
    else:
        import pymysql

        conn = _my(backend, RO, RO_PASSWORD, database=DB)
        try:
            with conn.cursor() as cur:
                for sql in DIRECT_WRITES:
                    with pytest.raises(pymysql.err.MySQLError) as caught:
                        cur.execute(sql)
                    assert caught.value.args[0] in (1142, 1143, 1044, 1227), (
                        sql,
                        caught.value,
                    )
        finally:
            conn.close()
    assert _count(backend, "c6t_orders") == before_rows
    assert _tables(backend) == before_tables


# == ⑧ timeouts end the query on the server ==========================================


def _short_deadline(monkeypatch, seconds: float) -> None:
    from opspilot.tools import sql_query

    original = sql_query.SqlTransport

    class Short(original):  # type: ignore[misc, valid-type]
        def __init__(self, config, *args, **kwargs):
            kwargs["request_timeout_seconds"] = seconds
            super().__init__(config, *args, **kwargs)

    import sys

    for name, module in list(sys.modules.items()):
        if (
            name.startswith("opspilot")
            and getattr(module, "SqlTransport", None) is original
        ):
            monkeypatch.setattr(module, "SqlTransport", Short)


def _running(backend: str, marker: str) -> int:
    if backend == "postgresql":
        return int(
            _admin_scalar(
                backend,
                "SELECT count(*) FROM pg_stat_activity WHERE query LIKE %s "
                "AND pid <> pg_backend_pid()",
                (f"%{marker}%",),
                database="postgres",
            )
        )
    return int(
        _admin_scalar(
            backend,
            "SELECT COUNT(*) FROM information_schema.processlist WHERE info LIKE %s "
            "AND id <> CONNECTION_ID()",
            (f"%{marker}%",),
        )
    )


def test_a_heavy_query_times_out_and_is_gone_from_the_server(
    backend, k8s_ca, monkeypatch
):
    _short_deadline(monkeypatch, 5.0)
    tool = Tool(_env(backend, k8s_ca))
    marker = _marker("timeout")
    started = time.monotonic()
    outcome = tool.query(
        f"SELECT /* {marker} */ COUNT(*) FROM c6t_bulk a, c6t_bulk b, c6t_bulk c, c6t_bulk d"
    )
    elapsed = time.monotonic() - started
    assert (outcome.status, outcome.reason) == ("timeout", "TOOL_TIMEOUT"), _view(
        outcome
    )
    assert elapsed < 15, elapsed
    end = time.monotonic() + 10
    while _running(backend, marker) and time.monotonic() < end:
        time.sleep(0.5)
    assert _running(backend, marker) == 0


# == ⑨ C6-U1: an administrator or over-privileged account is refused =================


def _pg_log() -> str:
    result = subprocess.run(
        ["docker", "logs", PG_CONTAINER], capture_output=True, text=True, check=True
    )
    return result.stdout + result.stderr


def _general_log_has(backend: str, marker: str) -> bool:
    # The general log also records this lookup: split the marker so its
    # own text never contains it contiguously.
    half = len(marker) // 2
    count = _admin_scalar(
        backend,
        "SELECT COUNT(*) FROM mysql.general_log "
        "WHERE CONVERT(argument USING utf8mb4) LIKE CONCAT('%%', %s, %s, '%%') "
        "AND CONVERT(argument USING utf8mb4) NOT LIKE '%%general_log%%'",
        (marker[:half], marker[half:]),
        database="mysql",
    )
    return int(count) > 0


def _server_saw(backend: str, marker: str) -> bool:
    if backend == "postgresql":
        return marker in _pg_log()
    return _general_log_has(backend, marker)


def _eventually(predicate, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.3)
    return predicate()


def test_administrator_account_is_refused_and_the_statement_never_arrives(
    backend, k8s_ca
):
    # Positive control: the server log does record what the tool sends.
    seen = _marker("u1-control")
    control = Tool(_env(backend, k8s_ca)).query(f"SELECT '{seen}' AS marker")
    _assert(control, "ok", None)
    assert _eventually(lambda: _server_saw(backend, seen)), (
        "statement log is not recording"
    )

    hidden = _marker("u1-admin")
    for tool in (QUERY, LIST_TABLES, DESCRIBE):
        params = {
            QUERY: {"sql": f"SELECT '{hidden}' AS marker"},
            LIST_TABLES: {},
            DESCRIBE: {"table_name": "c6t_orders"},
        }[tool]
        outcome = Tool(_admin_env(backend, k8s_ca)).run(tool, params)
        _assert(outcome, "error", "SOURCE_FORBIDDEN", "SQL_ACCOUNT_NOT_READ_ONLY")
    time.sleep(1.0)
    assert not _server_saw(backend, hidden)


@pytest.mark.parametrize("case", sorted(PG_BAD_ROLES))
def test_postgres_over_privileged_roles_are_refused(case, k8s_ca):
    _ensure("postgresql")
    marker = _marker(f"u1-pg-{case}")
    tool = Tool(_env("postgresql", k8s_ca, user=f"c6_tests_bad_{case}"))
    outcome = tool.query(f"SELECT '{marker}' AS marker")
    _assert(outcome, "error", "SOURCE_FORBIDDEN", "SQL_ACCOUNT_NOT_READ_ONLY")
    time.sleep(0.5)
    assert marker not in _pg_log()


def test_postgres_database_owner_is_refused(k8s_ca):
    _ensure("postgresql")
    tool = Tool(_env("postgresql", k8s_ca, user="c6_tests_owner", database=OWNED_DB))
    _assert(
        tool.query("SELECT 1 AS n"),
        "error",
        "SOURCE_FORBIDDEN",
        "SQL_ACCOUNT_NOT_READ_ONLY",
    )


def test_postgres_whitelisted_monitoring_roles_pass(k8s_ca):
    _ensure("postgresql")
    for user in ("c6_tests_ro_monitor", "c6_tests_ro_narrow"):
        outcome = Tool(_env("postgresql", k8s_ca, user=user)).query("SELECT 1 AS n")
        _assert(outcome, "ok", None)


@pytest.mark.parametrize("backend_name", ["mysql", "mariadb"])
@pytest.mark.parametrize("case", sorted(MY_BAD_USERS))
def test_mysql_over_privileged_accounts_are_refused(backend_name, case, k8s_ca):
    _ensure(backend_name)
    marker = _marker(f"u1-my-{case}")
    tool = Tool(_env(backend_name, k8s_ca, user=f"c6_tests_bad_{case}"))
    outcome = tool.query(f"SELECT '{marker}' AS marker")
    _assert(outcome, "error", "SOURCE_FORBIDDEN", "SQL_ACCOUNT_NOT_READ_ONLY")
    assert not _general_log_has(backend_name, marker)


# == ⑩ C6-8: OpsPilot's own databases ================================================


def test_a_worker_dsn_on_the_same_address_stops_the_worker(
    backend, k8s_ca, monkeypatch
):
    dsn = f"postgresql://opspilot:x@localhost:{PORT[backend]}/opspilot"
    monkeypatch.setenv("OPSPILOT_WORKER_DSN", dsn)
    env = {**_env(backend, k8s_ca), "OPSPILOT_WORKER_DSN": dsn}
    with pytest.raises(SystemExit, match="SQL_TARGETS_OPSPILOT_DB"):
        _executor(env)


def test_a_database_holding_opspilot_runs_is_refused(k8s_ca):
    _ensure("postgresql")
    marker = _marker("opsmock")
    tool = Tool(_env("postgresql", k8s_ca, database=OPSMOCK_DB))
    outcome = tool.query(f"SELECT '{marker}' AS marker")
    _assert(outcome, "error", "SOURCE_FORBIDDEN", "SQL_TARGETS_OPSPILOT_DB")
    time.sleep(0.5)
    assert marker not in _pg_log()


# == ⑪ error classification from real responses =====================================


def test_real_error_responses_are_classified(backend, ro, k8s_ca):
    _assert(ro.query("SELECT FROM WHERE"), "error", "INVALID_PARAMS")
    _assert(ro.query("SELECT * FROM c6t_nope"), "error", "INVALID_PARAMS")
    wrong = Tool(_env(backend, k8s_ca, password="wrong-" + "pw-570")).query("SELECT 1")
    _assert(wrong, "error", "SOURCE_FORBIDDEN")
    assert "wrong-pw-570" not in repr(_view(wrong))
    if backend == "postgresql":
        narrow = Tool(_env(backend, k8s_ca, user="c6_tests_ro_narrow"))
        _assert(
            narrow.query("SELECT * FROM c6t_customers"), "error", "SOURCE_FORBIDDEN"
        )
    else:
        _assert(ro.query("SELECT * FROM mysql.user"), "error", "SOURCE_FORBIDDEN")
    gone = Tool(
        {
            **_env(backend, k8s_ca),
            "OPSPILOT_SQL_URL": _url(backend, port=UNREACHABLE_PORT),
        }
    ).query("SELECT 1")
    _assert(gone, "error", "SOURCE_UNAVAILABLE")


def test_mysql_tls_modes_against_a_self_signed_server(k8s_ca):
    _ensure("mysql")
    required = Tool(_env("mysql", k8s_ca, OPSPILOT_SQL_TLS="require")).query(
        "SELECT 1 AS n"
    )
    _assert(required, "ok", None)
    verified = Tool(_env("mysql", k8s_ca, OPSPILOT_SQL_TLS="verify-full")).query(
        "SELECT 1 AS n"
    )
    _assert(verified, "error", "SOURCE_UNAVAILABLE")
