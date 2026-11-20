"""Implementer real-database tests for tool expansion C6a (#570), R8-25 and
the C6 pre-review C6-18 ... C6-21: the three ``database_sql_*`` tools
against real PostgreSQL 17, MySQL 8.0 and MariaDB 11 through the public tool
path. No model is called.

Runs only in the CI job ``db-backends`` (``OPSPILOT_DB_BACKENDS=1``), which
starts the containers and hands over admin credentials; skipped everywhere
else. The fixtures, not the product, write: with the admin account they
build the ``c6_impl`` database, synthetic tables and the read-only account
``c6_impl_ro`` (the C6-21 grants); the tool only uses that account, except
where a test configures the admin account on purpose (C6-U1, the session
layer). PostgreSQL logs every statement (``log_statement=all``) and
MySQL/MariaDB keep ``mysql.general_log``, which is how a statement is shown
never to have reached the server.
"""

from __future__ import annotations

import os
import ssl
import subprocess
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from opspilot.tools import sql_query as sq
from opspilot.tools.executor import TransportRequest, TransportTimeout
from opspilot.tools.registry import ToolContractError
from tests.test_sql_query import _execute, _view
from tests.test_tool_expansion_c5a_contract import FRAME

pytestmark = pytest.mark.skipif(
    os.environ.get("OPSPILOT_DB_BACKENDS") != "1",
    reason="real databases run only in the CI db-backends job",
)

HOST = os.environ.get("C6_DB_HOST", "127.0.0.1")
PORTS = {
    "postgresql": int(os.environ.get("C6_PG_PORT", "53201")),
    "mysql": int(os.environ.get("C6_MYSQL_PORT", "53202")),
    "mariadb": int(os.environ.get("C6_MARIADB_PORT", "53203")),
}
ADMINS = {
    "postgresql": os.environ.get("C6_PG_ADMIN_USER", "postgres"),
    "mysql": os.environ.get("C6_MYSQL_ADMIN_USER", "root"),
    "mariadb": os.environ.get("C6_MARIADB_ADMIN_USER", "root"),
}
ADMIN_PASSWORD = os.environ.get("C6_ADMIN_PASSWORD", "")
UNREACHABLE = int(os.environ.get("C6_UNREACHABLE_PORT", "53209"))
PG_CONTAINER = os.environ.get("C6_PG_CONTAINER", "c6-postgres")

DB = "c6_impl"
OPSPILOT_DB = "c6_impl_opspilot"
RO_USER = "c6_impl_ro"
RO_PASSWORD = "ro" + "only" + "570"
NOREAD_USER = "c6_impl_noread"
LONG = "L" * 5000
SECRET = "plain-" + "secret-" + "value-570"

KINDS = ("postgresql", "mysql", "mariadb")


# -- admin side ---------------------------------------------------------------


def _admin(kind: str, database: str | None = None) -> Any:
    if kind == "postgresql":
        import psycopg

        return psycopg.connect(
            host=HOST,
            port=PORTS[kind],
            user=ADMINS[kind],
            password=ADMIN_PASSWORD,
            dbname=database or "postgres",
            autocommit=True,
        )
    import pymysql

    return pymysql.connect(
        host=HOST,
        port=PORTS[kind],
        user=ADMINS[kind],
        password=ADMIN_PASSWORD,
        database=database,
        autocommit=True,
        ssl=_tls(kind),
    )


def _tls(kind: str) -> ssl.SSLContext | None:
    """MySQL 8's caching_sha2_password logs in over TLS without the
    ``cryptography`` package (the image's own self-signed certificate)."""
    if kind != "mysql":
        return None
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _run(kind: str, statements: list[str], database: str | None = None) -> None:
    conn = _admin(kind, database)
    try:
        with conn.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
    finally:
        conn.close()


def _scalar(kind: str, statement: str, database: str | None = DB) -> Any:
    conn = _admin(kind, database)
    try:
        with conn.cursor() as cursor:
            cursor.execute(statement)
            row = cursor.fetchone()
            return None if row is None else row[0]
    finally:
        conn.close()


def _seed_postgres() -> None:
    _run(
        "postgresql",
        [
            f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)",
            f"DROP DATABASE IF EXISTS {OPSPILOT_DB} WITH (FORCE)",
            f"DROP ROLE IF EXISTS {RO_USER}",
            f"DROP ROLE IF EXISTS {NOREAD_USER}",
            f"CREATE DATABASE {DB}",
            f"CREATE DATABASE {OPSPILOT_DB}",
            f"CREATE ROLE {RO_USER} LOGIN PASSWORD '{RO_PASSWORD}'",
            f"CREATE ROLE {NOREAD_USER} LOGIN PASSWORD '{RO_PASSWORD}'",
            f"GRANT CONNECT ON DATABASE {DB} TO {RO_USER}, {NOREAD_USER}",
            f"GRANT CONNECT ON DATABASE {OPSPILOT_DB} TO {RO_USER}",
            f"GRANT pg_read_all_data, pg_read_all_stats TO {RO_USER}",
            f"ALTER ROLE {RO_USER} SET default_transaction_read_only = on",
        ],
    )
    _run("postgresql", _tables("postgresql"), DB)
    _run(
        "postgresql",
        ["CREATE TABLE opspilot_runs (run_id uuid PRIMARY KEY)"],
        OPSPILOT_DB,
    )


def _seed_mysql(kind: str) -> None:
    _run(
        kind,
        [
            f"DROP DATABASE IF EXISTS {DB}",
            f"DROP USER IF EXISTS '{RO_USER}'@'%'",
            f"DROP USER IF EXISTS '{NOREAD_USER}'@'%'",
            f"CREATE DATABASE {DB}",
            f"CREATE USER '{RO_USER}'@'%' IDENTIFIED BY '{RO_PASSWORD}'",
            f"CREATE USER '{NOREAD_USER}'@'%' IDENTIFIED BY '{RO_PASSWORD}'",
            f"GRANT SELECT, SHOW VIEW ON {DB}.* TO '{RO_USER}'@'%'",
            f"GRANT SELECT ON performance_schema.* TO '{RO_USER}'@'%'",
            f"GRANT PROCESS ON *.* TO '{RO_USER}'@'%'",
        ],
    )
    _run(kind, _tables(kind), DB)
    # the no-read account may connect to the database but read no table
    _run(kind, [f"GRANT SHOW VIEW ON {DB}.* TO '{NOREAD_USER}'@'%'"])


def _tables(kind: str) -> list[str]:
    serial = "serial" if kind == "postgresql" else "INT AUTO_INCREMENT"
    text = "text"
    statements = [
        f"CREATE TABLE customers (id {serial} PRIMARY KEY, name {text} NOT NULL)",
        f"CREATE TABLE orders (id {serial} PRIMARY KEY, customer_id INT NOT NULL, "
        f"status VARCHAR(20) NOT NULL DEFAULT 'new', note {text}, "
        f"password {text}, api_token {text}, CONSTRAINT orders_customer_fk "
        "FOREIGN KEY (customer_id) REFERENCES customers (id))",
        "CREATE INDEX orders_status ON orders (status)",
        "CREATE VIEW v_orders AS SELECT id, status FROM orders",
        "CREATE TABLE big (id INT PRIMARY KEY, v INT NOT NULL)",
        "INSERT INTO customers (name) VALUES ('alice'), ('bob')",
        f"INSERT INTO orders (customer_id, status, note, password, api_token) VALUES "
        f"(1, 'paid', '{LONG}', '{SECRET}', '{SECRET}'), (2, 'new', 'short', "
        f"'{SECRET}', '{SECRET}')",
    ]
    if kind == "postgresql":
        statements.append(
            "INSERT INTO big SELECT g, g % 7 FROM generate_series(1, 600) g"
        )
    else:
        values = ", ".join(f"({i}, {i % 7})" for i in range(1, 601))
        statements.append(f"INSERT INTO big VALUES {values}")
    return statements


@pytest.fixture(scope="module", autouse=True)
def seeded() -> Iterator[None]:
    _seed_postgres()
    _seed_mysql("mysql")
    _seed_mysql("mariadb")
    yield


@pytest.fixture(autouse=True)
def ca_file(tmp_path, monkeypatch) -> None:
    ca = tmp_path / "ca.pem"
    ca.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("C5A_TEST_K8S_CA_FILE", str(ca))


# -- tool side ----------------------------------------------------------------


def _url(kind: str, database: str = DB, port: int | None = None) -> str:
    scheme = {"postgresql": "postgresql", "mysql": "mysql", "mariadb": "mariadb"}[kind]
    return f"{scheme}://{HOST}:{port or PORTS[kind]}/{database}"


def _env(
    kind: str,
    *,
    user: str = RO_USER,
    password: str = RO_PASSWORD,
    database: str = DB,
    port: int | None = None,
    tls: str | None = None,
) -> dict[str, str]:
    from tests.test_tool_expansion_c5a_contract import _base_env

    return {
        **_base_env(),
        sq.URL_ENV: _url(kind, database, port),
        sq.USERNAME_ENV: user,
        sq.PASSWORD_ENV: password,
        sq.TLS_ENV: tls or ("require" if kind == "mysql" else "disable"),
    }


def _call(kind: str, tool: str, params: dict[str, object], **env: Any):
    return _execute(_env(kind, **env), tool, params)


def _ok(outcome) -> dict[str, Any]:
    view = _view(outcome)
    assert outcome.status == "ok", view
    return view


def _count(kind: str, table: str = "orders") -> int:
    return int(_scalar(kind, f"SELECT COUNT(*) FROM {table}"))


def _marker() -> str:
    return "c6impl" + uuid.uuid4().hex[:12]


def _server_saw(kind: str, marker: str) -> bool:
    """Whether ``marker`` reached the server as statement text."""
    if kind == "postgresql":
        logs = subprocess.run(
            ["docker", "logs", PG_CONTAINER],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return marker in logs.stdout + logs.stderr
    found = _scalar(
        kind,
        "SELECT COUNT(*) FROM mysql.general_log WHERE argument LIKE "
        f"'%{marker}%' AND argument NOT LIKE '%general_log%'",
        None,
    )
    return int(found) > 0


# -- ① real data from every tool ---------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_three_tools_return_real_data(kind: str) -> None:
    rows = _ok(
        _call(kind, sq.QUERY_TOOL, {"sql": "SELECT id, status FROM orders ORDER BY id"})
    )["content"]
    assert [row["status"] for row in rows] == ["paid", "new"]
    listed = _ok(_call(kind, sq.LIST_TOOL, {}))["content"]
    names = {(row["name"], row["type"]) for row in listed}
    assert {("orders", "table"), ("v_orders", "view"), ("big", "table")} <= names
    no_views = _ok(_call(kind, sq.LIST_TOOL, {"include_views": False}))["content"]
    assert all(row["type"] != "view" for row in no_views)
    view = _ok(_call(kind, sq.DESCRIBE_TOOL, {"table_name": "orders"}))
    assert [c["name"] for c in view["content"]][:3] == ["id", "customer_id", "status"]
    assert view["primary_key"] == ["id"]
    assert view["foreign_keys"][0]["referred_table"] == "customers"
    assert any(index["columns"] == ["status"] for index in view["indexes"])
    status = next(c for c in view["content"] if c["name"] == "status")
    assert status["nullable"] is False and "new" in str(status["default"])


# -- ② plans ------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_explain_and_explain_analyze(kind: str) -> None:
    rows = _ok(
        _call(kind, sq.QUERY_TOOL, {"sql": "EXPLAIN SELECT * FROM orders WHERE id = 1"})
    )["content"]
    assert rows
    outcome = _call(
        kind,
        sq.QUERY_TOOL,
        {"sql": "EXPLAIN ANALYZE SELECT * FROM orders WHERE id = 1"},
    )
    if kind == "mariadb" and outcome.status != "ok":
        # MariaDB spells it ANALYZE SELECT, which the statement check refuses;
        # recorded, not asserted.
        print(f"mariadb EXPLAIN ANALYZE: {outcome.reason}")
    else:
        assert _ok(outcome)["content"]


# -- ③ limits and cuts -------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_limit_and_long_values(kind: str) -> None:
    view = _ok(_call(kind, sq.QUERY_TOOL, {"sql": "SELECT * FROM big", "limit": 100}))
    assert len(view["content"]) == 100 and view["incomplete_reason"]
    view = _ok(
        _call(kind, sq.QUERY_TOOL, {"sql": "SELECT note FROM orders WHERE id = 1"})
    )
    row = view["content"][0]
    assert len(row["note"]) == sq.MAX_VALUE_CHARS
    assert row["_truncated_fields"] == ["note"]
    repeat = "repeat('x', 3000000)" if kind == "postgresql" else "REPEAT('x', 3000000)"
    outcome = _call(kind, sq.QUERY_TOOL, {"sql": f"SELECT {repeat} AS big_value"})
    assert outcome.reason == "RESULT_TOO_LARGE", _view(outcome)


# -- ④ redaction by column name ---------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_secret_columns_redacted_alias_recorded(kind: str) -> None:
    view = _ok(
        _call(kind, sq.QUERY_TOOL, {"sql": "SELECT password, api_token FROM orders"})
    )
    assert SECRET not in str(view)
    aliased = _ok(
        _call(kind, sq.QUERY_TOOL, {"sql": "SELECT password AS x FROM orders"})
    )
    # residual risk (PRODUCT-CONSTRAINTS line 40): recorded, not asserted
    print(f"{kind}: aliased secret visible = {SECRET in str(aliased)}")


# -- ⑤ statement check on a real database ------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_statement_check_sends_nothing(kind: str) -> None:
    before = _count(kind)
    marker = _marker()
    for sql in (
        f"DELETE FROM orders /* {marker} */",
        f"SELECT 1; DELETE FROM orders /* {marker} */",
        f"WITH x AS (SELECT 1) SELECT * INTO t2 FROM orders /* {marker} */",
        f"SELECT * FROM orders FOR UPDATE /* {marker} */",
    ):
        outcome = _call(kind, sq.QUERY_TOOL, {"sql": sql})
        assert outcome.reason == "INVALID_PARAMS", _view(outcome)
    assert _count(kind) == before
    assert not _server_saw(kind, marker)


# -- ⑥ the read-only transaction alone ---------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_read_only_transaction_refuses_admin_write(kind: str, monkeypatch) -> None:
    monkeypatch.setattr(sq, "sql_statement_problem", lambda sql, dialect: None)
    monkeypatch.setattr(sq, "session_problem", lambda dialect, rows: None)
    before = _count(kind)
    outcome = _call(
        kind,
        sq.QUERY_TOOL,
        {"sql": "INSERT INTO customers (name) VALUES ('mallory')"},
        user=ADMINS[kind],
        password=ADMIN_PASSWORD,
    )
    assert outcome.reason == "SOURCE_FORBIDDEN", _view(outcome)
    assert "SESSION_READ_ONLY" in str(_view(outcome))
    assert _count(kind) == before
    assert _count(kind, "customers") == 2


# -- ⑦ the read-only account alone -------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_read_only_account_cannot_write_directly(kind: str) -> None:
    if kind == "postgresql":
        import psycopg

        def connect() -> Any:
            return psycopg.connect(
                host=HOST,
                port=PORTS[kind],
                user=RO_USER,
                password=RO_PASSWORD,
                dbname=DB,
                autocommit=True,
                options="-c default_transaction_read_only=off",
            )

        errors: tuple[type[BaseException], ...] = (psycopg.Error,)
    else:
        import pymysql

        def connect() -> Any:
            return pymysql.connect(
                host=HOST,
                port=PORTS[kind],
                user=RO_USER,
                password=RO_PASSWORD,
                database=DB,
                autocommit=True,
                ssl=_tls(kind),
            )

        errors = (pymysql.MySQLError,)
    before = _count(kind)
    for statement in (
        "INSERT INTO customers (name) VALUES ('mallory')",
        "UPDATE orders SET status = 'void'",
        "DELETE FROM orders",
        "CREATE TABLE evil (id INT)",
        "DROP TABLE big",
    ):
        conn = connect()
        try:
            with conn.cursor() as cursor, pytest.raises(errors):
                cursor.execute(statement)
        finally:
            conn.close()
    assert _count(kind) == before
    assert _count(kind, "big") == 600


# -- ⑧ timeouts --------------------------------------------------------------


def _request(tool: str, params: dict[str, object], timeout: float) -> TransportRequest:
    return TransportRequest(
        operation_id="c6impl",
        source="otel-demo",
        verb="query",
        endpoint="",
        selector={},
        params=params,
        window=FRAME,
        timeout_seconds=timeout,
        max_result_bytes=sq.READ_LIMIT_BYTES,
        credential_ref=sq.CREDENTIAL_REF,
        tool=tool,
    )


@pytest.mark.parametrize("kind", KINDS)
def test_timeout_cancels_the_statement(kind: str) -> None:
    marker = _marker()
    if kind == "postgresql":
        sql = f"SELECT count(*) FROM generate_series(1, 100000000000) /* {marker} */"
    else:
        sql = (
            "SELECT COUNT(*) FROM big a, big b, big c, big d WHERE "
            f"a.v + b.v + c.v + d.v = -1 /* {marker} */"
        )
    config = sq.SqlConfig(
        url=_url(kind),
        username=RO_USER,
        password=RO_PASSWORD,
        tls="require" if kind == "mysql" else "disable",
    )
    transport = sq.SqlTransport(config, request_timeout_seconds=4.0)
    started = time.monotonic()
    with pytest.raises(TransportTimeout):
        transport.fetch(_request(sq.QUERY_TOOL, {"sql": sql}, 4.0))
    assert time.monotonic() - started < 10
    time.sleep(2)
    if kind == "postgresql":
        running = _scalar(
            kind,
            "SELECT COUNT(*) FROM pg_stat_activity WHERE query LIKE "
            f"'%{marker}%' AND pid <> pg_backend_pid()",
        )
    else:
        running = _scalar(
            kind,
            "SELECT COUNT(*) FROM information_schema.processlist WHERE info LIKE "
            f"'%{marker}%' AND id <> CONNECTION_ID()",
        )
    assert int(running) == 0


# -- ⑨ the account check (C6-U1) ---------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_admin_account_refused_before_the_model_statement(kind: str) -> None:
    marker = _marker()
    outcome = _call(
        kind,
        sq.QUERY_TOOL,
        {"sql": f"SELECT 1 /* {marker} */"},
        user=ADMINS[kind],
        password=ADMIN_PASSWORD,
    )
    assert outcome.reason == "SOURCE_FORBIDDEN", _view(outcome)
    assert "SQL_ACCOUNT_NOT_READ_ONLY" in str(_view(outcome))
    assert not _server_saw(kind, marker)
    # the read-only account passes the same check
    marker = _marker()
    _ok(_call(kind, sq.QUERY_TOOL, {"sql": f"SELECT 1 AS one /* {marker} */"}))
    assert _server_saw(kind, marker)


# -- ⑩ OpsPilot's own database (C6-8) ----------------------------------------


def test_opspilot_database_refused() -> None:
    env = _env("postgresql")
    env["OPSPILOT_WORKER_DSN"] = (
        f"postgresql://w:x@localhost:{PORTS['postgresql']}/opspilot"
    )
    with pytest.raises(ToolContractError, match="SQL_TARGETS_OPSPILOT_DB"):
        sq.SqlConfig.from_env(env)
    marker = _marker()
    outcome = _call(
        "postgresql",
        sq.QUERY_TOOL,
        {"sql": f"SELECT 1 /* {marker} */"},
        database=OPSPILOT_DB,
    )
    assert outcome.reason == "SOURCE_FORBIDDEN", _view(outcome)
    assert "SQL_TARGETS_OPSPILOT_DB" in str(_view(outcome))
    assert not _server_saw("postgresql", marker)


# -- ⑪ error classes from real answers --------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_error_classes(kind: str) -> None:
    syntax = _call(kind, sq.QUERY_TOOL, {"sql": "SELECT FROM WHERE"})
    assert syntax.reason == "INVALID_PARAMS", _view(syntax)
    assert "SQL_INVALID" in str(_view(syntax))
    missing = _call(kind, sq.QUERY_TOOL, {"sql": "SELECT * FROM no_such_table"})
    assert missing.reason == "INVALID_PARAMS"
    assert "no_such_table" in str(_view(missing))
    wrong = _call(kind, sq.QUERY_TOOL, {"sql": "SELECT 1"}, password="wrong" + "570")
    assert wrong.reason == "SOURCE_FORBIDDEN", _view(wrong)
    noread = _call(
        kind, sq.QUERY_TOOL, {"sql": "SELECT * FROM orders"}, user=NOREAD_USER
    )
    assert noread.reason == "SOURCE_FORBIDDEN", _view(noread)
    down = _call(kind, sq.QUERY_TOOL, {"sql": "SELECT 1"}, port=UNREACHABLE)
    assert down.reason == "SOURCE_UNAVAILABLE", _view(down)
    empty = _call(kind, sq.QUERY_TOOL, {"sql": "SELECT * FROM orders WHERE id < 0"})
    assert empty.status == "no_data", _view(empty)
    absent = _call(kind, sq.DESCRIBE_TOOL, {"table_name": "no_such_table"})
    assert absent.reason == "INVALID_PARAMS"


def test_mysql_tls_modes() -> None:
    # MySQL 8 generates a self-signed certificate at first start.
    _ok(_call("mysql", sq.QUERY_TOOL, {"sql": "SELECT 1 AS one"}, tls="require"))
    refused = _call("mysql", sq.QUERY_TOOL, {"sql": "SELECT 1"}, tls="verify-full")
    assert refused.reason == "SOURCE_UNAVAILABLE", _view(refused)
