"""Implementer real-database tests for tool expansion C6b (#570): the three
``database_sql_*`` tools against real ClickHouse 24.1 (HTTP) and SQL Server
2022 through the public tool path. No model is called.

Runs only in the CI job ``db-backends`` (``OPSPILOT_DB_BACKENDS=1``), which
starts the containers (``c6-clickhouse``, ``c6-mssql``) and hands over admin
credentials; skipped everywhere else. The fixtures, not the product, write:
the admin account builds the ``c6b_impl`` database, synthetic tables and the
accounts below (the C6-21 grants); the tool uses the read-only account except
where a test configures another one on purpose.
"""

from __future__ import annotations

import http.client
import json
import os
import time
import urllib.parse
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
CH_PORT = int(os.environ.get("C6_CLICKHOUSE_PORT", "53204"))
MS_PORT = int(os.environ.get("C6_MSSQL_PORT", "53205"))
CH_ADMIN = os.environ.get("C6_CLICKHOUSE_ADMIN_USER", "default")
CH_ADMIN_PASSWORD = os.environ.get("C6_ADMIN_PASSWORD", "")
MS_ADMIN = os.environ.get("C6_MSSQL_ADMIN_USER", "sa")
MS_ADMIN_PASSWORD = os.environ.get("C6_MSSQL_ADMIN_PASSWORD", "")
UNREACHABLE = int(os.environ.get("C6_UNREACHABLE_PORT", "53209"))

DB = "c6b_impl"
RO = "c6b_impl_ro"
PASSWORD = "Ro" + "only" + "570b!"
SECRET = "plain-" + "secret-" + "value-570b"
LONG = "L" * 5000
KINDS = ("clickhouse", "mssql")


# -- admin side -----------------------------------------------------------------


def _ch(
    statement: str,
    *,
    user: str = CH_ADMIN,
    password: str = CH_ADMIN_PASSWORD,
    method: str = "POST",
    database: str | None = None,
) -> tuple[int, str]:
    conn = http.client.HTTPConnection(HOST, CH_PORT, timeout=60)
    params = {"database": database} if database else {}
    path = "/?" + urllib.parse.urlencode(params) if params else "/"
    headers = {"X-ClickHouse-User": user, "X-ClickHouse-Key": password}
    try:
        if method == "POST":
            conn.request("POST", path, body=statement.encode(), headers=headers)
        else:
            query = urllib.parse.urlencode({**params, "query": statement})
            conn.request("GET", "/?" + query, headers=headers)
        response = conn.getresponse()
        return response.status, response.read().decode("utf-8", "replace")
    finally:
        conn.close()


def _ch_ok(statement: str, **kwargs: Any) -> str:
    status, body = _ch(statement, **kwargs)
    assert status == 200, (statement, status, body)
    return body


def _ms(
    database: str = "master", user: str = MS_ADMIN, password: str = MS_ADMIN_PASSWORD
):
    import pytds

    return pytds.connect(
        server=HOST,
        port=MS_PORT,
        database=database,
        user=user,
        password=password,
        autocommit=True,
        login_timeout=30,
        timeout=60,
        disable_connect_retry=True,
    )


def _ms_run(statements: list[str], database: str = "master") -> None:
    conn = _ms(database)
    try:
        with conn.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement, None)
    finally:
        conn.close()


def _ms_scalar(statement: str, database: str = DB) -> Any:
    conn = _ms(database)
    try:
        with conn.cursor() as cursor:
            cursor.execute(statement, None)
            row = cursor.fetchone()
            return None if row is None else row[0]
    finally:
        conn.close()


def _seed_clickhouse() -> None:
    for user in (RO, "c6b_impl_ro1", "c6b_impl_writer"):
        _ch_ok(f"DROP USER IF EXISTS {user}")
    _ch_ok(f"DROP DATABASE IF EXISTS {DB}")
    _ch_ok(f"CREATE DATABASE {DB}")
    _ch_ok(
        f"CREATE TABLE {DB}.orders (id UInt32, status String DEFAULT 'new', "
        "note String, password String, api_token String, INDEX status_idx status "
        "TYPE bloom_filter GRANULARITY 1) ENGINE = MergeTree ORDER BY id"
    )
    _ch_ok(
        f"CREATE TABLE {DB}.big (id UInt32, v UInt32) ENGINE = MergeTree ORDER BY id"
    )
    _ch_ok(f"CREATE VIEW {DB}.v_orders AS SELECT id, status FROM {DB}.orders")
    _ch_ok(
        f"INSERT INTO {DB}.orders (id, status, note, password, api_token) VALUES "
        f"(1, 'paid', '{LONG}', '{SECRET}', '{SECRET}'), (2, 'new', 'short', "
        f"'{SECRET}', '{SECRET}')"
    )
    _ch_ok(f"INSERT INTO {DB}.big SELECT number + 1, number % 7 FROM numbers(600)")
    _ch_ok(
        f"CREATE USER {RO} IDENTIFIED WITH sha256_password BY '{PASSWORD}' SETTINGS readonly = 2"
    )
    _ch_ok(f"GRANT SELECT ON {DB}.* TO {RO}")
    _ch_ok(f"GRANT SELECT ON system.* TO {RO}")
    _ch_ok(
        f"CREATE USER c6b_impl_ro1 IDENTIFIED WITH sha256_password BY '{PASSWORD}' SETTINGS readonly = 1"
    )
    _ch_ok(f"GRANT SELECT ON {DB}.* TO c6b_impl_ro1")
    _ch_ok(
        f"CREATE USER c6b_impl_writer IDENTIFIED WITH sha256_password BY '{PASSWORD}'"
    )
    _ch_ok(f"GRANT SELECT, INSERT ON {DB}.* TO c6b_impl_writer")


def _seed_mssql() -> None:
    logins = (RO, "c6b_impl_writer", "c6b_impl_colupd", "c6b_impl_noread")
    _ms_run(
        [
            f"IF DB_ID('{DB}') IS NOT NULL BEGIN ALTER DATABASE {DB} SET SINGLE_USER "
            f"WITH ROLLBACK IMMEDIATE; DROP DATABASE {DB}; END",
            *(
                f"IF SUSER_ID('{login}') IS NOT NULL DROP LOGIN {login}"
                for login in logins
            ),
            f"CREATE DATABASE {DB}",
            *(
                f"CREATE LOGIN {login} WITH PASSWORD = '{PASSWORD}', CHECK_POLICY = OFF"
                for login in logins
            ),
            f"GRANT VIEW SERVER PERFORMANCE STATE TO {RO}",
        ]
    )
    values = ", ".join(f"({i}, {i % 7})" for i in range(1, 601))
    _ms_run(
        [
            "CREATE TABLE customers (id INT PRIMARY KEY, name NVARCHAR(50) NOT NULL)",
            "CREATE TABLE orders (id INT PRIMARY KEY, customer_id INT NOT NULL "
            "CONSTRAINT orders_customer_fk REFERENCES customers (id), status "
            "VARCHAR(20) NOT NULL CONSTRAINT orders_status_df DEFAULT 'new', note "
            "NVARCHAR(MAX), password NVARCHAR(100), api_token NVARCHAR(100))",
            "CREATE INDEX orders_status ON orders (status)",
            "CREATE TABLE big (id INT PRIMARY KEY, v INT NOT NULL)",
            "INSERT INTO customers VALUES (1, N'alice'), (2, N'bob')",
            f"INSERT INTO orders VALUES (1, 1, 'paid', N'{LONG}', N'{SECRET}', N'{SECRET}'), "
            f"(2, 2, 'new', N'short', N'{SECRET}', N'{SECRET}')",
            f"INSERT INTO big VALUES {values}",
            *(f"CREATE USER {login} FOR LOGIN {login}" for login in logins),
            f"ALTER ROLE db_datareader ADD MEMBER {RO}",
            f"GRANT SHOWPLAN TO {RO}",
            f"GRANT VIEW DATABASE STATE TO {RO}",
            "ALTER ROLE db_datareader ADD MEMBER c6b_impl_writer",
            "ALTER ROLE db_datawriter ADD MEMBER c6b_impl_writer",
            "ALTER ROLE db_datareader ADD MEMBER c6b_impl_colupd",
            "GRANT UPDATE (note) ON orders TO c6b_impl_colupd",
        ],
        DB,
    )
    _ms_run(["CREATE VIEW v_orders AS SELECT id, status FROM orders"], DB)


@pytest.fixture(scope="module", autouse=True)
def seeded() -> Iterator[None]:
    _seed_clickhouse()
    _seed_mssql()
    yield


@pytest.fixture(autouse=True)
def ca_file(tmp_path, monkeypatch) -> None:
    ca = tmp_path / "ca.pem"
    ca.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("C5A_TEST_K8S_CA_FILE", str(ca))


# -- tool side -------------------------------------------------------------------


def _url(kind: str, port: int | None = None) -> str:
    if kind == "clickhouse":
        return f"clickhouse+http://{HOST}:{port or CH_PORT}/{DB}"
    return f"mssql://{HOST}:{port or MS_PORT}/{DB}"


def _env(
    kind: str, *, user: str = RO, password: str = PASSWORD, port: int | None = None
):
    from tests.test_tool_expansion_c5a_contract import _base_env

    return {
        **_base_env(),
        sq.URL_ENV: _url(kind, port),
        sq.USERNAME_ENV: user,
        sq.PASSWORD_ENV: password,
        sq.TLS_ENV: "disable",
    }


def _call(kind: str, tool: str, params: dict[str, object], **env: Any):
    return _execute(_env(kind, **env), tool, params)


def _admin(kind: str) -> dict[str, str]:
    if kind == "clickhouse":
        return {"user": CH_ADMIN, "password": CH_ADMIN_PASSWORD}
    return {"user": MS_ADMIN, "password": MS_ADMIN_PASSWORD}


def _ok(outcome) -> dict[str, Any]:
    view = _view(outcome)
    assert outcome.status == "ok", view
    return view


def _count(kind: str, table: str = "orders") -> int:
    if kind == "clickhouse":
        return int(_ch_ok(f"SELECT count() FROM {DB}.{table}").strip())
    return int(_ms_scalar(f"SELECT COUNT(*) FROM {table}"))


def _marker() -> str:
    return "c6bimpl" + uuid.uuid4().hex[:12]


def _server_saw(kind: str, marker: str) -> bool:
    half = len(marker) // 2
    if kind == "clickhouse":
        _ch_ok("SYSTEM FLUSH LOGS")
        body = _ch_ok(
            "SELECT count() FROM system.query_log WHERE position(query, "
            f"concat('{marker[:half]}', '{marker[half:]}')) > 0"
        )
        return int(body.strip()) > 0
    found = _ms_scalar(
        "SELECT COUNT(*) FROM sys.dm_exec_query_stats qs CROSS APPLY "
        "sys.dm_exec_sql_text(qs.sql_handle) t WHERE CHARINDEX("
        f"'{marker[:half]}' + '{marker[half:]}', t.text) > 0",
        "master",
    )
    return int(found) > 0


# -- ① real data ------------------------------------------------------------------


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
    names_in_order = [c["name"] for c in view["content"]]
    assert names_in_order[0] == "id"
    assert view["primary_key"] == ["id"]
    status = next(c for c in view["content"] if c["name"] == "status")
    assert "new" in str(status["default"])
    if kind == "mssql":
        assert view["foreign_keys"][0]["referred_table"] == "customers"
        assert any(index["columns"] == ["status"] for index in view["indexes"])
    else:
        assert any(index["name"] == "status_idx" for index in view["indexes"])


def test_clickhouse_explain() -> None:
    rows = _ok(
        _call("clickhouse", sq.QUERY_TOOL, {"sql": "EXPLAIN SELECT * FROM orders"})
    )
    assert rows["content"]


def test_mssql_plan_from_dmv() -> None:
    sql = (
        "SELECT TOP 5 qs.execution_count, qp.query_plan FROM sys.dm_exec_query_stats "
        "qs CROSS APPLY sys.dm_exec_query_plan(qs.plan_handle) qp"
    )
    outcome = _call("mssql", sq.QUERY_TOOL, {"sql": sql})
    assert outcome.status in ("ok", "no_data"), _view(outcome)


# -- ③ ④ limits, cuts, redaction ---------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_limit_long_values_and_redaction(kind: str) -> None:
    view = _ok(_call(kind, sq.QUERY_TOOL, {"sql": "SELECT * FROM big", "limit": 100}))
    assert len(view["content"]) == 100 and view["incomplete_reason"]
    view = _ok(
        _call(kind, sq.QUERY_TOOL, {"sql": "SELECT note FROM orders WHERE id = 1"})
    )
    assert len(view["content"][0]["note"]) == sq.MAX_VALUE_CHARS
    view = _ok(
        _call(kind, sq.QUERY_TOOL, {"sql": "SELECT password, api_token FROM orders"})
    )
    assert SECRET not in json.dumps(view)


# -- ⑤ statement check on a real database ------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_statement_check_sends_nothing(kind: str) -> None:
    marker = _marker()
    if kind == "clickhouse":
        sqls = [
            f"SELECT * FROM url('http://127.0.0.1:9/x', CSV, 'a String') /* {marker} */",
            f"SELECT 1 SETTINGS max_execution_time = 999 /* {marker} */",
            f"SELECT 1 FORMAT CSV /* {marker} */",
            f"INSERT INTO orders (id) VALUES (9) /* {marker} */",
        ]
    else:
        sqls = [
            f"SELECT 1 EXEC('SELECT 2') /* {marker} */",
            f"SELECT 1 WAITFOR DELAY '00:00:01' /* {marker} */",
            f"SELECT * INTO orders_copy FROM orders /* {marker} */",
            f"SELECT 1 BEGIN TRANSACTION /* {marker} */",
        ]
    before = _count(kind)
    for sql in sqls:
        outcome = _call(kind, sq.QUERY_TOOL, {"sql": sql})
        assert outcome.reason == "INVALID_PARAMS", _view(outcome)
    assert _count(kind) == before
    assert not _server_saw(kind, marker)


# -- ⑥ the session layer alone ------------------------------------------------------


def test_clickhouse_get_is_read_only_even_for_the_admin(monkeypatch) -> None:
    before = _count("clickhouse")
    # the same admin account writes over POST ...
    _ch_ok(f"CREATE TABLE IF NOT EXISTS {DB}.probe (n UInt8) ENGINE = Memory")
    _ch_ok(f"INSERT INTO {DB}.probe VALUES (1)")
    assert int(_ch_ok(f"SELECT count() FROM {DB}.probe").strip()) >= 1
    # ... but the tool's GET is run read-only by the server
    monkeypatch.setattr(sq, "sql_statement_problem", lambda sql, dialect: None)
    monkeypatch.setattr(sq, "session_problem", lambda dialect, rows: None)
    outcome = _call(
        "clickhouse",
        sq.QUERY_TOOL,
        {"sql": "INSERT INTO orders (id, status) VALUES (99, 'x')"},
        **_admin("clickhouse"),
    )
    assert outcome.reason == "SOURCE_FORBIDDEN", _view(outcome)
    assert "SESSION_READ_ONLY" in json.dumps(_view(outcome))
    assert _count("clickhouse") == before


def test_mssql_writes_are_rolled_back(monkeypatch) -> None:
    monkeypatch.setattr(sq, "sql_statement_problem", lambda sql, dialect: None)
    monkeypatch.setattr(sq, "session_problem", lambda dialect, rows: None)
    before = _count("mssql", "customers")
    outcome = _call(
        "mssql",
        sq.QUERY_TOOL,
        {"sql": "INSERT INTO customers VALUES (99, N'mallory')"},
        **_admin("mssql"),
    )
    assert outcome.status != "ok" or not _view(outcome).get("content"), _view(outcome)
    assert _count("mssql", "customers") == before


# -- ⑦ the read-only account alone --------------------------------------------------


def test_read_only_accounts_cannot_write_directly() -> None:
    before = _count("clickhouse")
    status, body = _ch(
        f"INSERT INTO {DB}.orders (id) VALUES (77)", user=RO, password=PASSWORD
    )
    assert status != 200, body
    assert _count("clickhouse") == before
    import pytds

    before = _count("mssql")
    conn = _ms(DB, RO, PASSWORD)
    try:
        for statement in (
            "INSERT INTO customers VALUES (77, N'x')",
            "UPDATE orders SET status = 'void'",
            "DELETE FROM orders",
            "CREATE TABLE evil (id INT)",
            "DROP TABLE big",
        ):
            with conn.cursor() as cursor, pytest.raises(pytds.tds_base.Error):
                cursor.execute(statement, None)
    finally:
        conn.close()
    assert _count("mssql") == before and _count("mssql", "big") == 600


# -- ⑧ timeouts -------------------------------------------------------------------


def _request(params: dict[str, object], timeout: float) -> TransportRequest:
    return TransportRequest(
        operation_id="c6bimpl",
        source="otel-demo",
        verb="query",
        endpoint="",
        selector={},
        params=params,
        window=FRAME,
        timeout_seconds=timeout,
        max_result_bytes=sq.READ_LIMIT_BYTES,
        credential_ref=sq.CREDENTIAL_REF,
        tool=sq.QUERY_TOOL,
    )


@pytest.mark.parametrize("kind", KINDS)
def test_timeout_cancels_the_statement(kind: str) -> None:
    marker = _marker()
    if kind == "clickhouse":
        sql = (
            "SELECT count() FROM numbers(100000000000) WHERE "
            f"sipHash64(number) = 0 /* {marker} */"
        )
    else:
        sql = (
            "SELECT COUNT(*) FROM sys.all_objects a CROSS JOIN sys.all_objects b "
            "CROSS JOIN sys.all_objects c WHERE a.object_id + b.object_id + "
            f"c.object_id = -1 /* {marker} */"
        )
    config = sq.SqlConfig(url=_url(kind), username=RO, password=PASSWORD, tls="disable")
    transport = sq.SqlTransport(config, request_timeout_seconds=5.0)
    started = time.monotonic()
    with pytest.raises(TransportTimeout):
        transport.fetch(_request({"sql": sql}, 5.0))
    assert time.monotonic() - started < 12
    end = time.monotonic() + 10
    while time.monotonic() < end:
        if kind == "clickhouse":
            running = int(
                _ch_ok(
                    "SELECT count() FROM system.processes WHERE position(query, "
                    f"'{marker}') > 0 AND query NOT LIKE '%system.processes%'"
                ).strip()
            )
        else:
            running = int(
                _ms_scalar(
                    "SELECT COUNT(*) FROM sys.dm_exec_requests r CROSS APPLY "
                    "sys.dm_exec_sql_text(r.sql_handle) t WHERE CHARINDEX("
                    f"'{marker}', t.text) > 0 AND r.session_id <> @@SPID",
                    "master",
                )
            )
        if running == 0:
            break
        time.sleep(0.5)
    assert running == 0


# -- ⑨ account checks ------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_admin_account_refused_before_the_model_statement(kind: str) -> None:
    marker = _marker()
    outcome = _call(
        kind, sq.QUERY_TOOL, {"sql": f"SELECT 1 AS one /* {marker} */"}, **_admin(kind)
    )
    assert outcome.reason == "SOURCE_FORBIDDEN", _view(outcome)
    assert "SQL_ACCOUNT_NOT_READ_ONLY" in json.dumps(_view(outcome))
    assert not _server_saw(kind, marker)
    marker = _marker()
    _ok(_call(kind, sq.QUERY_TOOL, {"sql": f"SELECT 1 AS one /* {marker} */"}))
    if kind == "clickhouse":
        assert _server_saw(kind, marker)
    else:
        print(f"mssql plan cache saw the allowed marker: {_server_saw(kind, marker)}")


@pytest.mark.parametrize(
    ("kind", "user", "code"),
    [
        ("clickhouse", "c6b_impl_ro1", "SQL_ACCOUNT_UNSUPPORTED"),
        ("clickhouse", "c6b_impl_writer", "SQL_ACCOUNT_NOT_READ_ONLY"),
        ("mssql", "c6b_impl_writer", "SQL_ACCOUNT_NOT_READ_ONLY"),
        ("mssql", "c6b_impl_colupd", "SQL_ACCOUNT_NOT_READ_ONLY"),
    ],
)
def test_over_privileged_accounts_refused(kind: str, user: str, code: str) -> None:
    outcome = _call(kind, sq.QUERY_TOOL, {"sql": "SELECT 1 AS one"}, user=user)
    assert outcome.reason == "SOURCE_FORBIDDEN", _view(outcome)
    assert code in json.dumps(_view(outcome))


# -- ⑩ OpsPilot's own database --------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_opspilot_dsn_on_the_same_address(kind: str) -> None:
    env = _env(kind)
    port = CH_PORT if kind == "clickhouse" else MS_PORT
    env["OPSPILOT_WORKER_DSN"] = f"postgresql://w:x@localhost:{port}/opspilot"
    with pytest.raises(ToolContractError, match="SQL_TARGETS_OPSPILOT_DB"):
        sq.SqlConfig.from_env(env)


# -- ⑪ error classes ---------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_error_classes(kind: str) -> None:
    syntax = _call(kind, sq.QUERY_TOOL, {"sql": "SELECT FROM WHERE"})
    assert syntax.reason == "INVALID_PARAMS", _view(syntax)
    missing = _call(kind, sq.QUERY_TOOL, {"sql": "SELECT * FROM no_such_table"})
    assert missing.reason == "INVALID_PARAMS", _view(missing)
    assert "no_such_table" in json.dumps(_view(missing))
    wrong = _call(kind, sq.QUERY_TOOL, {"sql": "SELECT 1"}, password="Wrong" + "570b!")
    assert wrong.reason == "SOURCE_FORBIDDEN", _view(wrong)
    if kind == "mssql":
        noread = _call(
            kind, sq.QUERY_TOOL, {"sql": "SELECT * FROM orders"}, user="c6b_impl_noread"
        )
        assert noread.reason == "SOURCE_FORBIDDEN", _view(noread)
    down = _call(kind, sq.QUERY_TOOL, {"sql": "SELECT 1"}, port=UNREACHABLE)
    assert down.reason == "SOURCE_UNAVAILABLE", _view(down)
    empty = _call(kind, sq.QUERY_TOOL, {"sql": "SELECT * FROM orders WHERE id = 0"})
    assert empty.status == "no_data", _view(empty)
    absent = _call(kind, sq.DESCRIBE_TOOL, {"table_name": "no_such_table"})
    assert absent.reason == "INVALID_PARAMS", _view(absent)
