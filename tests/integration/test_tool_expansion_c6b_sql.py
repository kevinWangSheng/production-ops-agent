"""Real-server integration tests for tool expansion C6b (#570): the SQL
tools against real ClickHouse 24.1 and SQL Server 2022, through the public
tool path. No model is called.

Runs only in the CI job ``db-backends`` (``OPSPILOT_DB_BACKENDS=1``), after
the two C6a files and before the C6b implementer's file; skipped elsewhere.

Written by the independent test author from the C6 task record
(「C6b 公开接口」「CI（写死，测试作者与实现者共用）」, assertions ①-⑪),
without reading the implementation. The fixtures, not the product, write:
the administrator creates the database ``c6b_tests``, the synthetic rows,
the C6-21 read-only account ``c6b_tests_ro`` and the deliberately
over-privileged ``c6b_tests_*`` accounts. The tool gets the read-only
account except where an assertion is about the administrator (⑥, ⑨) or an
over-privileged account (C6-U1).

Neither server has a full read-only session: ClickHouse forces
``readonly=2`` on GET requests; SQL Server only has the tool's rollback
backstop. So the identity layer (⑦), the account check (⑨) and the
statement check (⑤) carry the read-only guarantee, and ⑥ shows what the
session layer does and does not undo.

CI environment (defaults in parentheses, fixed by the record):

* ``C6_DB_HOST`` (127.0.0.1); ``C6_CLICKHOUSE_PORT`` (53204),
  ``C6_MSSQL_PORT`` (53205); ``C6_UNREACHABLE_PORT`` (53209).
* ClickHouse administrator ``C6_CLICKHOUSE_ADMIN_USER`` (``default``) with
  ``C6_ADMIN_PASSWORD`` (``changeme570``); SQL Server ``C6_MSSQL_ADMIN_USER``
  (``sa``) with ``C6_MSSQL_ADMIN_PASSWORD`` (``Changeme570!``). CI-only
  low-entropy values.
* The tool reaches ClickHouse at ``clickhouse+http://<host>:<port>/<db>``
  and SQL Server at ``mssql://<host>:<port>/<db>``, both with
  ``OPSPILOT_SQL_TLS=disable``. Engineer-side ClickHouse calls are POSTs
  (writes, accounts); engineer-side SQL Server connections use python-tds
  without ``cafile``.
* Whether the statement reached the server (⑤, ⑨): ClickHouse
  ``system.query_log`` after ``SYSTEM FLUSH LOGS``; SQL Server the plan
  cache (``sys.dm_exec_query_stats`` x ``sys.dm_exec_sql_text``), with a
  positive control -- the record marks plan caching of these ad hoc
  statements 【CI 实测】; if the control fails, switch both files to
  ``sys.dm_exec_requests`` in-flight observation as the record says.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from opspilot.tools.profiles import PROFILE_ENV
from tests.integration.test_tool_expansion_c6a_sql import (
    Tool,
    _assert,
    _eventually,
    _executor,
    _marker,
    _notes,
    _rows,
    _view,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("OPSPILOT_DB_BACKENDS") != "1",
    reason="real databases run only in the CI db-backends job",
)

QUERY = "database_sql_query"
LIST_TABLES = "database_sql_list_tables"
DESCRIBE = "database_sql_describe_table"

HOST = os.environ.get("C6_DB_HOST", "127.0.0.1")
CH_PORT = int(os.environ.get("C6_CLICKHOUSE_PORT", "53204"))
MS_PORT = int(os.environ.get("C6_MSSQL_PORT", "53205"))
UNREACHABLE_PORT = int(os.environ.get("C6_UNREACHABLE_PORT", "53209"))
CH_ADMIN = os.environ.get("C6_CLICKHOUSE_ADMIN_USER", "default")
CH_ADMIN_PASSWORD = os.environ.get("C6_ADMIN_PASSWORD", "changeme570")
MS_ADMIN = os.environ.get("C6_MSSQL_ADMIN_USER", "sa")
MS_ADMIN_PASSWORD = os.environ.get("C6_MSSQL_ADMIN_PASSWORD", "Changeme570!")

DB = "c6b_tests"
PRIVATE_DB = "c6b_tests_private"
RO = "c6b_tests_ro"
RO_PASSWORD = "ro-" + "pw-570"  # low entropy, CI only
CANARY = "bbbbbbbb570"
BULK_ROWS = 600
LONG_CHARS = 5000
BIG_ROWS, BIG_CHARS = 500, 4500  # 2.25 MB before the 2000-char cut
WORDY_ROWS = 500

BACKENDS = ("clickhouse", "mssql")
PORT = {"clickhouse": CH_PORT, "mssql": MS_PORT}
SCHEME = {"clickhouse": "clickhouse+http", "mssql": "mssql"}
SCHEMA = {"clickhouse": DB, "mssql": "dbo"}
ADMIN = {
    "clickhouse": (CH_ADMIN, CH_ADMIN_PASSWORD),
    "mssql": (MS_ADMIN, MS_ADMIN_PASSWORD),
}


def _words(n: int) -> str:
    return " ".join(f"word{n}-{k} checkout latency spike" for k in range(55))


# -- ClickHouse, engineer side (POST; never the product) ---------------------------


class ClickHouseError(Exception):
    def __init__(self, status: int, code: str | None, body: str):
        super().__init__(f"HTTP {status} code={code}: {body[:300]}")
        self.status, self.code, self.body = status, code, body


def _ch_call(
    sql: str,
    *,
    user: str = CH_ADMIN,
    password: str = CH_ADMIN_PASSWORD,
    database: str | None = None,
    method: str = "POST",
    timeout: float = 120,
) -> str:
    params: dict[str, str] = {"default_format": "JSONCompactEachRow"}
    if database:
        params["database"] = database
    if method == "GET":
        params["query"] = sql
    url = f"http://{HOST}:{CH_PORT}/?{urlencode(params)}"
    request = Request(
        url,
        data=sql.encode() if method == "POST" else None,
        method=method,
        headers={"X-ClickHouse-User": user, "X-ClickHouse-Key": password},
    )
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.read().decode()
    except HTTPError as error:
        raise ClickHouseError(
            error.code,
            error.headers.get("X-ClickHouse-Exception-Code"),
            error.read().decode(errors="replace"),
        ) from None


def _ch_rows(sql: str, **kwargs: Any) -> list[list[Any]]:
    return [json.loads(line) for line in _ch_call(sql, **kwargs).splitlines() if line]


def _ch_scalar(sql: str, **kwargs: Any) -> Any:
    rows = _ch_rows(sql, **kwargs)
    return rows[0][0] if rows else None


# -- SQL Server, engineer side (python-tds, no cafile) -----------------------------


def _ms(
    database: str = "master", user: str = MS_ADMIN, password: str = MS_ADMIN_PASSWORD
):
    import pytds

    return pytds.connect(
        dsn=HOST,
        port=MS_PORT,
        database=database,
        user=user,
        password=password,
        autocommit=True,
        login_timeout=30,
        timeout=120,
    )


def _ms_exec(statements: list[str] | str, database: str = DB, params=None) -> None:
    if isinstance(statements, str):
        statements = [statements]
    conn = _ms(database)
    try:
        with conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement, params)
    finally:
        conn.close()


def _ms_scalar(sql: str, params: tuple | None = None, database: str = DB) -> Any:
    conn = _ms(database)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


# -- shared observations --------------------------------------------------------------


def _count(backend: str, table: str) -> int:
    if backend == "clickhouse":
        return int(_ch_scalar(f"SELECT count() FROM {DB}.{table}"))
    return int(_ms_scalar(f"SELECT COUNT(*) FROM dbo.{table}"))


def _tables(backend: str) -> list[str]:
    if backend == "clickhouse":
        return [
            r[0]
            for r in _ch_rows(
                f"SELECT name FROM system.tables WHERE database = '{DB}' ORDER BY name"
            )
        ]
    conn = _ms(DB)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.name + '.' + o.name FROM sys.objects o JOIN sys.schemas s "
                "ON s.schema_id = o.schema_id WHERE o.type IN ('U', 'V') ORDER BY 1"
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def _seq_value() -> int:
    return int(
        _ms_scalar(
            "SELECT CAST(current_value AS BIGINT) FROM sys.sequences WHERE name = 'c6t_seq'"
        )
    )


def _ch_saw(marker: str) -> bool:
    _ch_call("SYSTEM FLUSH LOGS")
    a, b = marker[: len(marker) // 2], marker[len(marker) // 2 :]
    return (
        int(
            _ch_scalar(
                "SELECT count() FROM system.query_log WHERE query LIKE "
                f"concat('%', '{a}', '{b}', '%') AND query NOT LIKE '%query_log%'"
            )
        )
        > 0
    )


def _ms_saw(marker: str) -> bool:
    """The marker in the plan cache (query stats, or any cached plan --
    ad hoc shells included). The lookup splits the marker and names the
    DMVs, so it never matches itself; CHARINDEX avoids ``%`` under
    python-tds' pyformat parameters."""
    half = len(marker) // 2
    return (
        int(
            _ms_scalar(
                "SELECT (SELECT COUNT(*) FROM sys.dm_exec_query_stats qs "
                "CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st "
                "WHERE CHARINDEX(%s + %s, st.text) > 0 "
                "AND CHARINDEX(N'dm_exec_', st.text) = 0) + "
                "(SELECT COUNT(*) FROM sys.dm_exec_cached_plans cp "
                "CROSS APPLY sys.dm_exec_sql_text(cp.plan_handle) st "
                "WHERE CHARINDEX(%s + %s, st.text) > 0 "
                "AND CHARINDEX(N'dm_exec_', st.text) = 0)",
                (marker[:half], marker[half:], marker[:half], marker[half:]),
                database="master",
            )
        )
        > 0
    )


def _server_saw(backend: str, marker: str) -> bool:
    return _ch_saw(marker) if backend == "clickhouse" else _ms_saw(marker)


def _running(backend: str, marker: str) -> int:
    a, b = marker[: len(marker) // 2], marker[len(marker) // 2 :]
    if backend == "clickhouse":
        return int(
            _ch_scalar(
                "SELECT count() FROM system.processes WHERE query LIKE "
                f"concat('%', '{a}', '{b}', '%') AND query NOT LIKE '%system.processes%'"
            )
        )
    return int(
        _ms_scalar(
            "SELECT COUNT(*) FROM sys.dm_exec_requests r "
            "CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) st "
            "WHERE CHARINDEX(%s + %s, st.text) > 0 AND r.session_id <> @@SPID",
            (a, b),
            database="master",
        )
    )


# -- fixtures: schema, data and accounts (C6-21) -------------------------------------

#: ClickHouse accounts the account check must refuse: suffix -> grants.
CH_BAD_USERS: dict[str, list[str]] = {
    "insert": ["GRANT SELECT, INSERT ON c6b_tests.* TO {u}"],
    "all_db": ["GRANT ALL ON c6b_tests.* TO {u}"],
    "grant_option": ["GRANT SELECT ON c6b_tests.* TO {u} WITH GRANT OPTION"],
    "alter_update": ["GRANT SELECT, ALTER UPDATE ON c6b_tests.* TO {u}"],
    "create_table": ["GRANT SELECT, CREATE TABLE ON c6b_tests.* TO {u}"],
    "url_source": ["GRANT SELECT ON c6b_tests.* TO {u}", "GRANT URL ON *.* TO {u}"],
    "kill_query": [
        "GRANT SELECT ON c6b_tests.* TO {u}",
        "GRANT KILL QUERY ON *.* TO {u}",
    ],
    "access_mgmt": [
        "GRANT SELECT ON c6b_tests.* TO {u}",
        "GRANT ACCESS MANAGEMENT ON *.* TO {u}",
    ],
    "role": ["GRANT SELECT ON c6b_tests.* TO {u}", "GRANT c6b_tests_writer TO {u}"],
    "partial_revoke": [
        "GRANT SELECT ON *.* TO {u}",
        "REVOKE SELECT ON system.* FROM {u}",
    ],
}
CH_SETUP_ERROR: dict[str, str] = {}

#: SQL Server accounts the account check must refuse:
#: suffix -> (server-level statements, database-level statements or None
#: for "no database user").
MS_BAD_USERS: dict[str, tuple[list[str], list[str] | None]] = {
    "sysadmin": (["ALTER SERVER ROLE sysadmin ADD MEMBER [{u}]"], None),
    "bulkadmin": (["ALTER SERVER ROLE bulkadmin ADD MEMBER [{u}]"], []),
    "server_perm": (["GRANT ALTER ANY LOGIN TO [{u}]"], []),
    "db_owner": ([], ["ALTER ROLE db_owner ADD MEMBER [{u}]"]),
    "datawriter": ([], ["ALTER ROLE db_datawriter ADD MEMBER [{u}]"]),
    "ddladmin": ([], ["ALTER ROLE db_ddladmin ADD MEMBER [{u}]"]),
    "db_execute": ([], ["GRANT EXECUTE TO [{u}]"]),
    "object_insert": ([], ["GRANT INSERT ON dbo.c6t_orders TO [{u}]"]),
    "column_update": ([], ["GRANT UPDATE (note) ON dbo.c6t_orders TO [{u}]"]),
    "schema_alter": ([], ["GRANT ALTER ON SCHEMA::dbo TO [{u}]"]),
    "owns_schema": ([], ["CREATE SCHEMA c6b_tests_owned AUTHORIZATION [{u}]"]),
    "role_writer": ([], ["ALTER ROLE c6b_tests_writer ADD MEMBER [{u}]"]),
}
MS_SETUP_ERROR: dict[str, str] = {}


def _ch_setup() -> None:
    users = [RO, "c6b_tests_ro1", *(f"c6b_tests_bad_{n}" for n in CH_BAD_USERS)]
    _ch_call(f"DROP USER IF EXISTS {', '.join(users)}")
    _ch_call("DROP ROLE IF EXISTS c6b_tests_writer")
    for db in (DB, PRIVATE_DB):
        _ch_call(f"DROP DATABASE IF EXISTS {db} SYNC")
        _ch_call(f"CREATE DATABASE {db}")
    statements = [
        f"""CREATE TABLE {DB}.c6t_customers (
            id UInt32, name String, email Nullable(String),
            password String, api_token String,
            created_at DateTime DEFAULT now()
        ) ENGINE = MergeTree ORDER BY id""",
        f"""CREATE TABLE {DB}.c6t_orders (
            id UInt32, customer_id UInt32, amount Decimal(10, 2),
            status String DEFAULT 'new', note Nullable(String),
            INDEX c6t_orders_customer_idx customer_id TYPE minmax GRANULARITY 1
        ) ENGINE = MergeTree ORDER BY id""",
        f"CREATE VIEW {DB}.c6t_order_totals AS SELECT customer_id, sum(amount) AS total "
        f"FROM {DB}.c6t_orders GROUP BY customer_id",
        f"CREATE MATERIALIZED VIEW {DB}.c6t_order_mv ENGINE = MergeTree ORDER BY customer_id "
        f"AS SELECT customer_id, amount FROM {DB}.c6t_orders",
        f"CREATE TABLE {DB}.c6t_bulk (n UInt32, label String) ENGINE = MergeTree ORDER BY n",
        f"INSERT INTO {DB}.c6t_bulk SELECT number + 1, concat('row-', toString(number + 1)) "
        f"FROM numbers({BULK_ROWS})",
        f"CREATE TABLE {DB}.c6t_long (id UInt32, body String) ENGINE = MergeTree ORDER BY id",
        f"INSERT INTO {DB}.c6t_long SELECT 1, repeat('L', {LONG_CHARS})",
        f"CREATE TABLE {DB}.c6t_big (id UInt32, body String) ENGINE = MergeTree ORDER BY id",
        f"INSERT INTO {DB}.c6t_big SELECT number + 1, repeat('b', {BIG_CHARS}) "
        f"FROM numbers({BIG_ROWS})",
        f"CREATE TABLE {DB}.c6t_wordy (id UInt32, note String) ENGINE = MergeTree ORDER BY id",
        f"INSERT INTO {DB}.c6t_wordy SELECT number, arrayStringConcat(arrayMap(k -> "
        f"concat('word', toString(number), '-', toString(k), ' checkout latency spike'), "
        f"range(55)), ' ') FROM numbers({WORDY_ROWS})",
        f"INSERT INTO {DB}.c6t_customers (id, name, email, password, api_token) VALUES "
        + ", ".join(
            f"({n}, 'customer-{n}', 'c{n}@example.invalid', 'pw-{CANARY}', 'tok-{CANARY}')"
            for n in range(1, 4)
        ),
        f"INSERT INTO {DB}.c6t_orders (id, customer_id, amount, status, note) VALUES "
        + ", ".join(
            f"({n + 1}, {1 + n % 3}, {10 + n}, 'paid', 'order-{n}')" for n in range(5)
        ),
        f"CREATE TABLE {PRIVATE_DB}.c6t_secret (id UInt32) ENGINE = MergeTree ORDER BY id",
        f"INSERT INTO {PRIVATE_DB}.c6t_secret VALUES (1)",
        # C6-21 read-only account.
        f"CREATE USER {RO} IDENTIFIED WITH sha256_password BY '{RO_PASSWORD}' "
        "SETTINGS readonly = 2",
        f"GRANT SELECT ON {DB}.* TO {RO}",
        f"GRANT SELECT ON system.* TO {RO}",
        # The same grants with readonly = 1 (SQL_ACCOUNT_UNSUPPORTED).
        f"CREATE USER c6b_tests_ro1 IDENTIFIED WITH sha256_password BY '{RO_PASSWORD}' "
        "SETTINGS readonly = 1",
        f"GRANT SELECT ON {DB}.* TO c6b_tests_ro1",
        "GRANT SELECT ON system.* TO c6b_tests_ro1",
        "CREATE ROLE c6b_tests_writer",
        f"GRANT INSERT ON {DB}.* TO c6b_tests_writer",
    ]
    for statement in statements:
        _ch_call(statement)
    CH_SETUP_ERROR.clear()
    for name, grants in CH_BAD_USERS.items():
        user = f"c6b_tests_bad_{name}"
        try:
            _ch_call(
                f"CREATE USER {user} IDENTIFIED WITH sha256_password BY '{RO_PASSWORD}'"
            )
            for grant in grants:
                _ch_call(grant.format(u=user))
        except Exception as error:  # reported by that case, not swallowed
            CH_SETUP_ERROR[name] = f"{type(error).__name__}: {error}"


def _ms_setup() -> None:
    logins = [RO, *(f"c6b_tests_bad_{n}" for n in MS_BAD_USERS)]
    master = [
        f"IF DB_ID(N'{DB}') IS NOT NULL BEGIN "
        f"ALTER DATABASE [{DB}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
        f"DROP DATABASE [{DB}]; END",
        *(
            f"IF SUSER_ID(N'{login}') IS NOT NULL DROP LOGIN [{login}]"
            for login in logins
        ),
        f"CREATE DATABASE [{DB}]",
        *(
            f"CREATE LOGIN [{login}] WITH PASSWORD = N'{RO_PASSWORD}', CHECK_POLICY = OFF"
            for login in logins
        ),
        # C6-21 server-level grant.
        f"GRANT VIEW SERVER PERFORMANCE STATE TO [{RO}]",
    ]
    _ms_exec(master, database="master")
    _ms_exec(
        [
            """CREATE TABLE dbo.c6t_customers (
                id INT IDENTITY(1, 1) CONSTRAINT c6t_customers_pk PRIMARY KEY,
                name NVARCHAR(100) NOT NULL,
                email NVARCHAR(200) NULL CONSTRAINT c6t_customers_email_uq UNIQUE,
                password NVARCHAR(200) NULL,
                api_token NVARCHAR(200) NULL,
                created_at DATETIME2 NOT NULL
                    CONSTRAINT c6t_customers_created_df DEFAULT SYSUTCDATETIME()
            )""",
            """CREATE TABLE dbo.c6t_orders (
                id INT IDENTITY(1, 1) CONSTRAINT c6t_orders_pk PRIMARY KEY,
                customer_id INT NOT NULL CONSTRAINT c6t_orders_customer_fk
                    REFERENCES dbo.c6t_customers (id),
                amount DECIMAL(10, 2) NOT NULL,
                status NVARCHAR(20) NULL CONSTRAINT c6t_orders_status_df DEFAULT 'new',
                note NVARCHAR(200) NULL
            )""",
            "CREATE INDEX c6t_orders_customer_idx ON dbo.c6t_orders (customer_id)",
            "CREATE VIEW dbo.c6t_order_totals AS SELECT customer_id, SUM(amount) AS total "
            "FROM dbo.c6t_orders GROUP BY customer_id",
            "CREATE TABLE dbo.c6t_bulk (n INT PRIMARY KEY, label NVARCHAR(40))",
            f"INSERT INTO dbo.c6t_bulk SELECT n, CONCAT('row-', n) FROM (SELECT TOP ({BULK_ROWS}) "
            "ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS n FROM sys.all_objects a "
            "CROSS JOIN sys.all_objects b) x",
            "CREATE TABLE dbo.c6t_long (id INT PRIMARY KEY, body NVARCHAR(MAX))",
            f"INSERT INTO dbo.c6t_long VALUES (1, REPLICATE(CAST(N'L' AS NVARCHAR(MAX)), {LONG_CHARS}))",
            "CREATE TABLE dbo.c6t_big (id INT PRIMARY KEY, body NVARCHAR(MAX))",
            f"INSERT INTO dbo.c6t_big SELECT n, REPLICATE(CAST(N'b' AS NVARCHAR(MAX)), {BIG_CHARS}) "
            f"FROM (SELECT TOP ({BIG_ROWS}) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS n "
            "FROM sys.all_objects a CROSS JOIN sys.all_objects b) x",
            "CREATE TABLE dbo.c6t_wordy (id INT PRIMARY KEY, note NVARCHAR(MAX))",
            "CREATE TABLE dbo.c6t_secret (id INT PRIMARY KEY)",
            "INSERT INTO dbo.c6t_secret VALUES (1)",
            "CREATE SEQUENCE dbo.c6t_seq AS BIGINT START WITH 1 INCREMENT BY 1",
            # C6-21 database-level grants.
            f"CREATE USER [{RO}] FOR LOGIN [{RO}]",
            f"ALTER ROLE db_datareader ADD MEMBER [{RO}]",
            f"GRANT SHOWPLAN TO [{RO}]",
            f"GRANT VIEW DATABASE STATE TO [{RO}]",
            # A table the read-only account may not read (⑪).
            f"DENY SELECT ON dbo.c6t_secret TO [{RO}]",
            "CREATE ROLE c6b_tests_writer",
            "GRANT INSERT ON dbo.c6t_orders TO c6b_tests_writer",
        ]
    )
    conn = _ms(DB)
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO dbo.c6t_wordy VALUES (%s, %s)",
                [(n, _words(n)) for n in range(WORDY_ROWS)],
            )
            cur.executemany(
                "INSERT INTO dbo.c6t_customers (name, email, password, api_token) "
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
                "INSERT INTO dbo.c6t_orders (customer_id, amount, status, note) "
                "VALUES (%s, %s, %s, %s)",
                [(1 + n % 3, 10 + n, "paid", f"order-{n}") for n in range(5)],
            )
    finally:
        conn.close()
    MS_SETUP_ERROR.clear()
    for name, (server, database) in MS_BAD_USERS.items():
        login = f"c6b_tests_bad_{name}"
        try:
            if server:
                _ms_exec([s.format(u=login) for s in server], database="master")
            if database is not None:
                _ms_exec(
                    [
                        f"CREATE USER [{login}] FOR LOGIN [{login}]",
                        f"ALTER ROLE db_datareader ADD MEMBER [{login}]",
                        *(s.format(u=login) for s in database),
                    ]
                )
        except Exception as error:  # reported by that case, not swallowed
            MS_SETUP_ERROR[name] = f"{type(error).__name__}: {error}"


_READY: set[str] = set()


def _ensure(name: str) -> str:
    if name not in _READY:
        (_ch_setup if name == "clickhouse" else _ms_setup)()
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
    path = tmp_path_factory.mktemp("c6b-k8s") / "synthetic-ca.pem"
    path.write_text("synthetic ca placeholder\n")
    return str(path)


# -- the product path ---------------------------------------------------------------


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
        "OPSPILOT_SQL_TLS": "disable",
    }
    values.update(extra)
    return {k: v for k, v in values.items() if v != ""}


def _admin_env(backend: str, k8s_ca: str, **extra: str) -> dict[str, str]:
    user, password = ADMIN[backend]
    return _env(backend, k8s_ca, user=user, password=password, **extra)


@pytest.fixture
def ro(backend, k8s_ca) -> Tool:
    return Tool(_env(backend, k8s_ca))


def _skip_checks(monkeypatch) -> None:
    # ASSUMPTION: as in C6a ⑥, the transport looks both seams up on the
    # module at call time.
    from opspilot.tools import sql_query

    monkeypatch.setattr(sql_query, "sql_statement_problem", lambda sql, dialect: None)
    monkeypatch.setattr(sql_query, "session_problem", lambda dialect, rows: None)


# == ① real data from all three tools =============================================


def test_query_returns_real_rows_as_column_value_maps(backend, ro):
    order = "ORDER BY id"
    outcome = ro.query(f"SELECT id, name, email FROM c6t_customers {order}")
    _assert(outcome, "ok", None)
    rows = _rows(outcome)
    assert [r["name"] for r in rows] == ["customer-1", "customer-2", "customer-3"]
    assert [k for k in rows[0] if k != "_truncated_fields"] == ["id", "name", "email"]
    view = _view(outcome)
    assert view["dialect"] == backend
    assert view["incomplete"] is False
    assert view["source_start_at"] is None and view["source_end_at"] is None


def test_list_tables_returns_tables_and_views_sorted(backend, ro):
    schema = SCHEMA[backend]
    outcome = ro.run(LIST_TABLES, {})
    _assert(outcome, "ok", None)
    everything = _rows(outcome)
    by_name = {r["name"]: r for r in everything if str(r["name"]).startswith("c6t_")}
    assert by_name["c6t_customers"] == {
        "schema": schema,
        "name": "c6t_customers",
        "type": "table",
    }
    assert by_name["c6t_order_totals"]["type"] == "view"
    if backend == "clickhouse":
        assert by_name["c6t_order_mv"]["type"] == "materialized_view"
    assert {"c6t_orders", "c6t_bulk", "c6t_long", "c6t_big", "c6t_wordy"} <= set(
        by_name
    )
    keys = [(str(r["schema"]), str(r["name"])) for r in everything]
    if backend == "clickhouse":
        assert keys == sorted(keys)
    else:  # server collation decides the order of punctuation; schema groups
        assert [k[0] for k in keys] == sorted(k[0] for k in keys)
    for row in everything:
        assert row["type"] in {"table", "view", "materialized_view"}, row
        if backend == "mssql":
            assert row["type"] in {"table", "view"}
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
    indexes = {i["name"]: i for i in view["indexes"]}
    assert "c6t_orders_customer_idx" in indexes
    if backend == "clickhouse":
        assert view["foreign_keys"] == []
        sorting = indexes["sorting_key"]
        assert sorting["primary"] is True and sorting["unique"] is False
        assert sorting["columns"] == ["id"]
        assert str(sorting["definition"]).strip() == "id"
    else:
        [fk] = view["foreign_keys"]
        assert fk["columns"] == ["customer_id"]
        assert fk["referred_table"] == "c6t_customers"
        assert fk["referred_columns"] == ["id"]
        index = indexes["c6t_orders_customer_idx"]
        assert index["columns"] == ["customer_id"] and index["unique"] is False
        assert index["definition"] is None
        assert any(i["primary"] for i in view["indexes"])
    schema = SCHEMA[backend]
    for other in (
        ro.run(DESCRIBE, {"table_name": f"{schema}.c6t_orders"}),
        ro.run(DESCRIBE, {"table_name": "c6t_orders", "schema": schema}),
    ):
        _assert(other, "ok", None)
        assert [c["name"] for c in _rows(other)] == [c["name"] for c in columns]
    view_columns = ro.run(DESCRIBE, {"table_name": "c6t_order_totals"})
    _assert(view_columns, "ok", None)
    assert [c["name"] for c in _rows(view_columns)] == ["customer_id", "total"]
    missing = ro.run(DESCRIBE, {"table_name": "c6t_nope"})
    _assert(missing, "error", "INVALID_PARAMS", "TABLE_NOT_FOUND")


# == ② plans ===========================================================================


def test_clickhouse_explain_returns_a_plan(k8s_ca):
    _ensure("clickhouse")
    tool = Tool(_env("clickhouse", k8s_ca))
    for sql in (
        "EXPLAIN SELECT * FROM c6t_orders WHERE customer_id = 1",
        "EXPLAIN PIPELINE SELECT count() FROM c6t_bulk",
    ):
        outcome = tool.query(sql)
        _assert(outcome, "ok", None)
        assert _rows(outcome), sql


def test_mssql_plan_from_the_dmvs(k8s_ca):
    _ensure("mssql")
    tool = Tool(_env("mssql", k8s_ca))
    _assert(tool.query("SELECT COUNT(*) AS n FROM c6t_bulk WHERE n > 10"), "ok", None)
    outcome = tool.query(
        "SELECT TOP 5 qp.query_plan FROM sys.dm_exec_query_stats qs "
        "CROSS APPLY sys.dm_exec_query_plan(qs.plan_handle) qp "
        "WHERE qp.query_plan IS NOT NULL"
    )
    _assert(outcome, "ok", None)
    assert "showplan" in repr(_rows(outcome)).lower()


# == ③ limit, long values, size limits ===============================================


def test_limit_truncates_and_marks_incomplete(backend, ro):
    outcome = ro.query("SELECT n, label FROM c6t_bulk ORDER BY n", limit=500)
    _assert(outcome, "ok", None)
    assert [r["n"] for r in _rows(outcome)] == list(range(1, 501))
    view = _view(outcome)
    assert view["incomplete"] is True
    assert "limit" in str(view["incomplete_reason"]).lower()
    whole = ro.query("SELECT n FROM c6t_bulk WHERE n <= 7 ORDER BY n", limit=7)
    _assert(whole, "ok", None)
    assert _view(whole)["incomplete"] is False


def test_long_values_are_cut(backend, ro):
    outcome = ro.query("SELECT id, body FROM c6t_long")
    _assert(outcome, "ok", None)
    [row] = _rows(outcome)
    assert row["body"].startswith("L" * 1000) and len(row["body"]) < LONG_CHARS
    assert "L" * 2001 not in repr(_view(outcome))
    assert row["_truncated_fields"] == ["body"]


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
    aliased = ro.query("SELECT password AS p, api_token AS t FROM c6t_customers")
    leaked = CANARY in repr(_view(aliased))
    record_property(
        f"c6b_alias_redaction_{backend}", "leaked" if leaked else "redacted"
    )
    print(
        f"C6B-EVIDENCE alias redaction on {backend}: {'leaked' if leaked else 'redacted'}"
    )


# == ⑤ the statement check on a real server ==========================================

REFUSED_ON_REAL = {
    "clickhouse": [
        "SELECT 1 /* {m} */ SETTINGS max_execution_time = 0",
        "SELECT /* {m} */ 1 FORMAT TSV",
        "SELECT /* {m} */ * FROM url('http://127.0.0.1:1/x', CSV, 'a String')",
        "SELECT /* {m} */ * FROM file('/etc/passwd', 'LineAsString')",
        "SELECT /* {m} */ * FROM remote('127.0.0.1', system.one)",
        "SELECT /* {m} */ * FROM executable('x.sh', TSV, 'a String')",
        "SELECT /* {m} */ * FROM s3('http://127.0.0.1:1/b/x.csv', CSV)",
        "SELECT /* {m} */ sleepEachRow(1) FROM numbers(3)",
        "SELECT /* {m} */ sleep(2)",
        "ALTER TABLE c6t_orders DELETE WHERE 1 /* {m} */",
        "INSERT INTO c6t_orders (id, customer_id, amount) VALUES (99, 1, 1) /* {m} */",
        "SELECT 1 /* {m} */; DROP TABLE c6t_orders",
        "KILL QUERY WHERE 1 /* {m} */",
        "OPTIMIZE TABLE c6t_orders FINAL /* {m} */",
        "SYSTEM FLUSH LOGS /* {m} */",
        r"SELECT /* {m} */ 'a\'' SETTINGS max_threads = 1 --'",
        "SELECT /* {m} */ 1 # SETTINGS max_threads = 1",
    ],
    "mssql": [
        "SELECT '{m}' AS m EXEC xp_cmdshell 'whoami'",
        "SELECT '{m}' AS m WAITFOR DELAY '00:00:03'",
        "DECLARE @x int = 1 SELECT '{m}' AS m",
        "SELECT '{m}' AS m INTO dbo.c6t_copy FROM dbo.c6t_orders",
        "BEGIN TRAN SELECT '{m}' AS m COMMIT",
        "SELECT '{m}' AS m COMMIT",
        "SELECT NEXT VALUE FOR dbo.c6t_seq AS v, '{m}' AS m",
        "SELECT '{m}' AS m; DELETE FROM dbo.c6t_orders",
        "SELECT '{m}' AS m\nGO\nDELETE FROM dbo.c6t_orders",
        "USE master SELECT '{m}' AS m",
        "SELECT '{m}' AS m DBCC FREEPROCCACHE",
        "SELECT * FROM OPENROWSET(BULK '/etc/hostname', SINGLE_CLOB) AS x WHERE '{m}' = ''",
        r"SELECT 'a\' DELETE FROM dbo.c6t_orders --' + '{m}'",
        "SELECT [it's] FROM dbo.c6t_orders WAITFOR DELAY '00:00:03' --'{m}",
    ],
}


def test_refused_statements_change_nothing_and_never_arrive(backend, ro):
    before_rows = _count(backend, "c6t_orders")
    before_tables = _tables(backend)
    before_seq = _seq_value() if backend == "mssql" else None
    markers = []
    for template in REFUSED_ON_REAL[backend]:
        marker = _marker("refused")
        markers.append(marker)
        sql = template.replace("{m}", marker)
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
    if backend == "mssql":
        assert _seq_value() == before_seq
    time.sleep(1.0)
    for marker in markers:
        assert not _server_saw(backend, marker), marker


def test_clickhouse_table_functions_are_also_denied_to_the_read_only_account():
    # The identity layer for table functions: even sent directly (GET, as the
    # tool would), the C6-21 account has no source privileges.
    _ensure("clickhouse")
    for sql in (
        "SELECT * FROM file('/etc/passwd', 'LineAsString')",
        "SELECT * FROM url('http://127.0.0.1:1/x', CSV, 'a String')",
        "SELECT * FROM remote('127.0.0.1', system.one)",
    ):
        for method in ("GET", "POST"):
            with pytest.raises(ClickHouseError) as caught:
                _ch_call(sql, user=RO, password=RO_PASSWORD, database=DB, method=method)
            assert caught.value.status != 200, sql


# == ⑥ the session layer with the checks skipped ======================================


def test_clickhouse_get_forces_readonly_2_and_refuses_an_administrator_insert(
    k8s_ca, monkeypatch
):
    _ensure("clickhouse")
    ro = Tool(_env("clickhouse", k8s_ca))
    probe = ro.query("SELECT getSetting('readonly') AS r")
    _assert(probe, "ok", None)
    assert int(_rows(probe)[0]["r"]) == 2
    _skip_checks(monkeypatch)
    admin = Tool(_admin_env("clickhouse", k8s_ca))
    forced = admin.query("SELECT getSetting('readonly') AS r")
    _assert(forced, "ok", None)
    assert int(_rows(forced)[0]["r"]) == 2  # account readonly=0, GET -> 2
    before_rows, before_tables = (
        _count("clickhouse", "c6t_orders"),
        _tables("clickhouse"),
    )
    for sql in (
        "INSERT INTO c6t_orders (id, customer_id, amount, status, note) "
        "VALUES (77, 1, 1, 'x', 'c6b-readonly')",
        f"CREATE TABLE {DB}.c6t_new (a UInt8) ENGINE = Memory",
        "ALTER TABLE c6t_orders DELETE WHERE 1",
    ):
        outcome = admin.query(sql)
        # Only the server answers SESSION_READ_ONLY (code 164): the write was
        # sent and readonly=2 refused it.
        assert (outcome.status, outcome.reason) == ("error", "SOURCE_FORBIDDEN"), (
            sql,
            _view(outcome),
        )
        assert "SESSION_READ_ONLY" in " ".join(_notes(outcome)), _view(outcome)
    assert _count("clickhouse", "c6t_orders") == before_rows
    assert _tables("clickhouse") == before_tables


def test_mssql_rollback_backstop_undoes_only_transactional_writes(k8s_ca, monkeypatch):
    _ensure("mssql")
    _skip_checks(monkeypatch)
    admin = Tool(_admin_env("mssql", k8s_ca))
    before_rows = _count("mssql", "c6t_orders")
    before_notes = _ms_scalar(
        "SELECT STRING_AGG(note, ',') WITHIN GROUP (ORDER BY id) FROM dbo.c6t_orders"
    )
    before_tables = _tables("mssql")
    for sql in (
        "INSERT INTO dbo.c6t_orders (customer_id, amount, status, note) "
        "VALUES (1, 1, 'x', 'c6b-rollback')",
        "UPDATE dbo.c6t_orders SET note = 'c6b-rolled-back'",
        "CREATE TABLE dbo.c6t_rolled (a INT)",
    ):
        outcome = admin.query(sql)
        # Not "ok with data": the write produced no result set to report.
        assert not (outcome.status == "ok" and _view(outcome).get("content")), (
            sql,
            _view(outcome),
        )
    # Transactional writes are undone by ROLLBACK TRANSACTION.
    assert _count("mssql", "c6t_orders") == before_rows
    after_notes = _ms_scalar(
        "SELECT STRING_AGG(note, ',') WITHIN GROUP (ORDER BY id) FROM dbo.c6t_orders"
    )
    assert after_notes == before_notes
    assert _tables("mssql") == before_tables
    # A non-transactional side effect is not: a sequence number stays taken.
    # This is why the statement check refuses NEXT VALUE (and the record says
    # the rollback is only a backstop).
    before_seq = _seq_value()
    taken = admin.query("SELECT NEXT VALUE FOR dbo.c6t_seq AS v")
    _assert(taken, "ok", None)
    assert _seq_value() > before_seq
    print(
        f"C6B-EVIDENCE mssql sequence advanced under rollback: {before_seq} -> {_seq_value()}"
    )


# == ⑦ the identity layer: the read-only account itself cannot write ================

CH_DIRECT_WRITES = [
    "INSERT INTO c6t_orders (id, customer_id, amount) VALUES (88, 1, 1)",
    "ALTER TABLE c6t_orders UPDATE note = 'x' WHERE 1",
    "ALTER TABLE c6t_orders DELETE WHERE 1",
    f"CREATE TABLE {DB}.c6t_direct (a UInt8) ENGINE = Memory",
    f"DROP TABLE {DB}.c6t_orders",
    f"TRUNCATE TABLE {DB}.c6t_orders",
]
MS_DIRECT_WRITES = [
    "INSERT INTO dbo.c6t_orders (customer_id, amount, status, note) VALUES (1, 1, 'x', 'c6b-direct')",
    "UPDATE dbo.c6t_orders SET amount = 0",
    "DELETE FROM dbo.c6t_orders",
    "CREATE TABLE dbo.c6t_direct (a INT)",
    "DROP TABLE dbo.c6t_orders",
    "EXEC xp_cmdshell 'whoami'",
    "SELECT NEXT VALUE FOR dbo.c6t_seq",
]


def test_read_only_account_cannot_write_when_connected_directly(
    backend, record_property
):
    before_rows = _count(backend, "c6t_orders")
    before_tables = _tables(backend)
    seen: list[str] = []
    if backend == "clickhouse":
        for sql in CH_DIRECT_WRITES:
            for method in ("POST", "GET"):
                with pytest.raises(ClickHouseError) as caught:
                    _ch_call(
                        sql, user=RO, password=RO_PASSWORD, database=DB, method=method
                    )
                assert caught.value.code in ("164", "497"), (sql, method, caught.value)
                seen.append(f"{method} {caught.value.code}")
    else:
        import pytds

        before_seq = _seq_value()
        conn = _ms(DB, RO, RO_PASSWORD)
        try:
            with conn.cursor() as cur:
                for sql in MS_DIRECT_WRITES:
                    with pytest.raises(pytds.Error) as caught:
                        cur.execute(sql)
                    seen.append(str(getattr(caught.value, "msg_no", "?")))
        finally:
            conn.close()
        assert _seq_value() == before_seq
    record_property(f"c6b_direct_write_errors_{backend}", ",".join(seen))
    assert _count(backend, "c6t_orders") == before_rows
    assert _tables(backend) == before_tables


# == ⑧ timeouts end the query on the server ==========================================

HEAVY = {
    "clickhouse": "SELECT /* {m} */ count() FROM numbers_mt(1000000000000) "
    "WHERE (number * 7919) % 1000003 = 1",
    "mssql": "SELECT COUNT_BIG(*) AS n FROM sys.all_objects a CROSS JOIN sys.all_objects b "
    "CROSS JOIN sys.all_objects c CROSS JOIN sys.all_objects d "
    "WHERE a.object_id + b.object_id + c.object_id + d.object_id = -1 AND '{m}' <> ''",
}


def test_a_heavy_query_times_out_and_is_gone_from_the_server(
    backend, k8s_ca, monkeypatch
):
    from tests.c6b_sql_support import set_request_timeout

    set_request_timeout(monkeypatch, 5.0)
    tool = Tool(_env(backend, k8s_ca))
    marker = _marker("timeout")
    started = time.monotonic()
    outcome = tool.query(HEAVY[backend].replace("{m}", marker))
    elapsed = time.monotonic() - started
    assert (outcome.status, outcome.reason) == ("timeout", "TOOL_TIMEOUT"), _view(
        outcome
    )
    assert elapsed < 15, elapsed
    end = time.monotonic() + 10
    while _running(backend, marker) and time.monotonic() < end:
        time.sleep(0.5)
    assert _running(backend, marker) == 0


# == ⑨ C6-U1: administrators and over-privileged accounts are refused ===============


def _costly(marker: str, backend: str) -> str:
    """A statement the server certainly logs/caches, carrying ``marker``."""
    if backend == "clickhouse":
        return f"SELECT '{marker}' AS marker"
    return (
        "SELECT COUNT(*) AS n FROM sys.all_objects a CROSS JOIN sys.all_objects b "
        f"WHERE a.name = N'{marker}'"
    )


def test_administrator_account_is_refused_and_the_statement_never_arrives(
    backend, k8s_ca
):
    seen = _marker("u1-control")
    control = Tool(_env(backend, k8s_ca)).query(_costly(seen, backend))
    _assert(control, "ok", None)
    assert _eventually(lambda: _server_saw(backend, seen), 10), (
        "the server-side record does not see what the tool sends; "
        "per the record, switch ⑨ to the in-flight observation"
    )
    hidden = _marker("u1-admin")
    for tool in (QUERY, LIST_TABLES, DESCRIBE):
        params = {
            QUERY: {"sql": _costly(hidden, backend)},
            LIST_TABLES: {},
            DESCRIBE: {"table_name": "c6t_orders"},
        }[tool]
        outcome = Tool(_admin_env(backend, k8s_ca)).run(tool, params)
        _assert(outcome, "error", "SOURCE_FORBIDDEN", "SQL_ACCOUNT_NOT_READ_ONLY")
    time.sleep(1.0)
    assert not _server_saw(backend, hidden)


def test_clickhouse_readonly_1_account_is_unsupported(k8s_ca):
    _ensure("clickhouse")
    marker = _marker("u1-ro1")
    tool = Tool(_env("clickhouse", k8s_ca, user="c6b_tests_ro1"))
    outcome = tool.query(_costly(marker, "clickhouse"))
    _assert(outcome, "error", "SOURCE_FORBIDDEN", "SQL_ACCOUNT_UNSUPPORTED")
    assert "readonly" in " ".join(_notes(outcome)).lower()
    assert not _ch_saw(marker)


@pytest.mark.parametrize("case", sorted(CH_BAD_USERS))
def test_clickhouse_over_privileged_accounts_are_refused(case, k8s_ca):
    _ensure("clickhouse")
    if case in CH_SETUP_ERROR:
        pytest.fail(f"fixture for {case} failed: {CH_SETUP_ERROR[case]}")
    marker = _marker(f"u1-ch-{case}")
    tool = Tool(_env("clickhouse", k8s_ca, user=f"c6b_tests_bad_{case}"))
    outcome = tool.query(_costly(marker, "clickhouse"))
    _assert(outcome, "error", "SOURCE_FORBIDDEN", "SQL_ACCOUNT_NOT_READ_ONLY")
    assert not _ch_saw(marker)


@pytest.mark.parametrize("case", sorted(MS_BAD_USERS))
def test_mssql_over_privileged_accounts_are_refused(case, k8s_ca):
    _ensure("mssql")
    if case in MS_SETUP_ERROR:
        pytest.fail(f"fixture for {case} failed: {MS_SETUP_ERROR[case]}")
    marker = _marker(f"u1-ms-{case}")
    tool = Tool(_env("mssql", k8s_ca, user=f"c6b_tests_bad_{case}"))
    outcome = tool.query(_costly(marker, "mssql"))
    _assert(outcome, "error", "SOURCE_FORBIDDEN", "SQL_ACCOUNT_NOT_READ_ONLY")
    time.sleep(0.5)
    assert not _ms_saw(marker)


def test_the_c6_21_read_only_accounts_pass_the_check(backend, ro):
    # Positive control for ⑨: the record's grant list is accepted as is.
    _assert(ro.query("SELECT 1 AS n"), "ok", None)


# == ⑩ C6-8: OpsPilot's own databases ================================================


def test_a_worker_dsn_on_the_same_address_stops_the_worker(
    backend, k8s_ca, monkeypatch
):
    dsn = f"postgresql://opspilot:x@localhost:{PORT[backend]}/opspilot"
    monkeypatch.setenv("OPSPILOT_WORKER_DSN", dsn)
    env = {**_env(backend, k8s_ca), "OPSPILOT_WORKER_DSN": dsn}
    with pytest.raises(SystemExit, match="SQL_TARGETS_OPSPILOT_DB"):
        _executor(env)


# == ⑪ error classification from real responses =====================================


def test_real_error_responses_are_classified(backend, ro, k8s_ca):
    _assert(ro.query("SELECT FROM WHERE"), "error", "INVALID_PARAMS")
    _assert(ro.query("SELECT * FROM c6t_nope"), "error", "INVALID_PARAMS")
    wrong = Tool(_env(backend, k8s_ca, password="wrong-" + "pw-570")).query(
        "SELECT 1 AS n"
    )
    _assert(wrong, "error", "SOURCE_FORBIDDEN")
    assert "wrong-pw-570" not in repr(_view(wrong))
    if backend == "clickhouse":
        denied = ro.query(f"SELECT * FROM {PRIVATE_DB}.c6t_secret")
    else:
        denied = ro.query("SELECT * FROM dbo.c6t_secret")
    _assert(denied, "error", "SOURCE_FORBIDDEN")
    gone = Tool(
        {
            **_env(backend, k8s_ca),
            "OPSPILOT_SQL_URL": _url(backend, port=UNREACHABLE_PORT),
        }
    ).query("SELECT 1 AS n")
    _assert(gone, "error", "SOURCE_UNAVAILABLE")


def test_views_never_carry_the_credentials(backend, k8s_ca):
    outcomes = [
        Tool(_env(backend, k8s_ca)).query("SELECT 1 AS n"),
        Tool(_env(backend, k8s_ca)).query("SELECT FROM WHERE"),
        Tool(_admin_env(backend, k8s_ca)).query("SELECT 1 AS n"),
    ]
    rendered = json.dumps([o.model_view for o in outcomes], default=str)
    assert RO_PASSWORD not in rendered
    assert ADMIN[backend][1] not in rendered
