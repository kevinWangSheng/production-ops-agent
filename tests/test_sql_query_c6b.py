"""Implementer unit tests for the C6b SQL dialects, ClickHouse and SQL Server
(#570). The independent contract tests are the acceptance suite; these pin
the implementer's reversible choices through the public profile path with a
recording fake connector, and the ClickHouse HTTP shape against a local
``http.server``. No database, no model. Credentials are low-entropy
placeholders.
"""

from __future__ import annotations

import json
import shutil
import threading
from collections.abc import Iterator, Sequence
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest

from opspilot.tools import ToolRequest
from opspilot.tools import otel_demo_ext as ext
from opspilot.tools import sql_query as sq
from opspilot.tools.profiles import select_profile
from opspilot.tools.registry import ToolContractError
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import NOW, TARGET_ID, FakeStore, _lease
from tests.test_tool_expansion_c5a_contract import FRAME, _base_env

PASSWORD = "readonly" + "570b"
CH_URL = "clickhouse+http://127.0.0.1:58123/shop"
MS_URL = "mssql://127.0.0.1:51433/shop"
CH_GRANTS = [("GRANT SELECT ON shop.* TO ro",), ("GRANT SELECT ON system.* TO ro",)]


class Script:
    def __init__(self, dialect: str) -> None:
        self.dialect = dialect
        self.sent: list[str] = []
        self.grants: list[tuple[Any, ...]] = list(CH_GRANTS)
        self.readonly = "2"
        self.problems: list[tuple[Any, ...]] = []
        self.result: tuple[list[str], list[tuple[Any, ...]]] = (["n"], [(1,)])
        self.fail: Exception | None = None

    def connector(self, config: sq.SqlConfig, timeout: float) -> Any:
        script = self

        class Conn:
            server_version = ""

            def execute(self, statement: str, params: Sequence[object] = ()) -> None:
                script.sent.append(statement)

            def query(
                self, statement: str, params: Sequence[object] = (), *, max_rows: int
            ):
                script.sent.append(statement)
                if statement == sq._CLICKHOUSE_GRANTS_CHECK:
                    return ["g"], list(script.grants), False
                if statement == sq._CLICKHOUSE_READONLY_CHECK:
                    return ["r", "v"], [(script.readonly, "24.1")], False
                if statement.startswith(sq.ACCOUNT_CHECK_MARKER):
                    return ["p"], list(script.problems), False
                if script.fail is not None:
                    raise script.fail
                columns, rows = script.result
                return columns, rows[:max_rows], len(rows) > max_rows

            def cancel(self) -> None:
                script.sent.append("<cancel>")

            def close(self) -> None:
                script.sent.append("<close>")

        return Conn()


@pytest.fixture
def fake(monkeypatch, tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("C5A_TEST_K8S_CA_FILE", str(ca))

    def make(dialect: str) -> Script:
        script = Script(dialect)
        monkeypatch.setattr(sq, "default_connector", script.connector)
        return script

    return make


def _env(url: str, **extra: str) -> dict[str, str]:
    return {
        **_base_env(),
        sq.URL_ENV: url,
        sq.USERNAME_ENV: "ro",
        sq.PASSWORD_ENV: PASSWORD,
        sq.TLS_ENV: "disable",
        **extra,
    }


def _execute(env: dict[str, str], tool: str, params: dict[str, object]):
    if shutil.which("jq") is None or shutil.which("python3") is None:
        pytest.skip("the extension worker needs jq and python3 on PATH")
    clock = FakeClock(start=NOW)
    profile = select_profile(env)
    lease = _lease()
    deadline = NOW + timedelta(seconds=600)
    factory = profile.executor_factory(
        FakeStore(lease, deadline=deadline), RecordingSink(), clock
    )
    model_input = profile.face(clock).input_for(
        run_id=str(lease.run_id),
        question="Why is checkout slow?",
        target_id=TARGET_ID,
        deadline=deadline,
        model_requests=12,
    )
    return factory(lease, model_input).execute(
        ToolRequest(
            step_id="c6b-step",
            tool_index=0,
            tool_name=tool,
            target_ref=TARGET_ID,
            params=params,
            window=FRAME.as_json(),
        )
    )


def _view(outcome) -> dict[str, Any]:
    assert outcome.model_view is not None
    return dict(outcome.model_view)


# -- configuration ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "tls", "ok"),
    [
        (CH_URL, "disable", True),
        (CH_URL, "require", False),
        ("clickhouse+https://127.0.0.1/shop", "verify-full", True),
        ("clickhouse+https://127.0.0.1/shop", "require", True),
        ("clickhouse+https://127.0.0.1/shop", "disable", False),
        (MS_URL, "disable", True),
        ("mssql+pytds://127.0.0.1/shop", "disable", True),
        (MS_URL, "require", False),
        (MS_URL, "verify-full", False),
    ],
)
def test_tls_follows_the_dialect(url: str, tls: str, ok: bool) -> None:
    env = {
        sq.URL_ENV: url,
        sq.USERNAME_ENV: "ro",
        sq.PASSWORD_ENV: "x",
        sq.TLS_ENV: tls,
    }
    if ok:
        assert sq.SqlConfig.from_env(env) is not None
    else:
        with pytest.raises(ToolContractError, match="SQL_CONFIG_INVALID"):
            sq.SqlConfig.from_env(env)


def test_default_ports_and_revisions() -> None:
    env = {
        sq.URL_ENV: "clickhouse+https://h/db",
        sq.USERNAME_ENV: "u",
        sq.PASSWORD_ENV: "x",
    }
    assert sq.SqlConfig.from_env(env).port == 8443  # type: ignore[union-attr]
    env[sq.URL_ENV] = "mssql://h/db"
    env[sq.TLS_ENV] = "disable"
    assert sq.SqlConfig.from_env(env).port == 1433  # type: ignore[union-attr]
    revisions = {
        ext.tool_schema_revision(sql=d)
        for d in ("postgresql", "mysql", "clickhouse", "mssql")
    }
    assert len(revisions) == 4
    assert ext.SQL_POSTGRESQL_TOOL_SCHEMA_REVISION == "otel-demo-ext-01d9ced4706c"
    assert ext.SQL_MYSQL_TOOL_SCHEMA_REVISION == "otel-demo-ext-b8a4a763a94f"
    assert sq.sql_dialect({sq.URL_ENV: CH_URL}) == "clickhouse"
    assert sq.sql_dialect({sq.URL_ENV: MS_URL}) == "mssql"


def test_opspilot_dsn_on_the_same_address_is_refused() -> None:
    env = {
        sq.URL_ENV: "mssql://127.0.0.1:5432/shop",
        sq.USERNAME_ENV: "ro",
        sq.PASSWORD_ENV: "x",
        sq.TLS_ENV: "disable",
        "OPSPILOT_WORKER_DSN": "host=localhost port=5432 dbname=o",
    }
    with pytest.raises(ToolContractError, match="SQL_TARGETS_OPSPILOT_DB"):
        sq.SqlConfig.from_env(env)


# -- statement check ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        (
            "clickhouse",
            "SELECT * FROM `system`.query_log ORDER BY event_time DESC LIMIT 5",
        ),
        ("clickhouse", "SELECT count() FROM shop.orders WHERE status = 'it\\'s'"),
        ("clickhouse", "EXPLAIN PIPELINE SELECT 1"),
        ("clickhouse", 'SELECT "url", `file` FROM t'),
        ("clickhouse", "SHOW TABLES"),
        ("mssql", "SELECT TOP 5 * FROM [dbo].[orders]"),
        ("mssql", "SELECT N'it''s', 'C:\\'"),
        ("mssql", "SELECT 1 /* a /* nested */ b */"),
        ("mssql", "SELECT * FROM sys.dm_exec_requests"),
        ("mssql", "WITH x AS (SELECT 1 AS n) SELECT n FROM x"),
        ("mssql", "SELECT [begin], [exec] FROM t"),
        ("mssql", "SELECT * FROM t WITH (NOLOCK)"),
        ("mssql", "SELECT [updlock], rowlock_id FROM t WHERE c = 'HOLDLOCK'"),
    ],
)
def test_new_dialects_allow(dialect: str, sql: str) -> None:
    assert sq.sql_statement_problem(sql, dialect) is None


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("clickhouse", "SELECT * FROM url('http://x/a.csv', CSV)"),
        ("clickhouse", "SELECT * FROM s3Cluster('c', 'x')"),
        ("clickhouse", "SELECT * FROM remote('h', db, t)"),
        ("clickhouse", "SELECT * FROM input('a String')"),
        ("clickhouse", "SELECT * FROM executable('x', CSV, 'a String')"),
        ("clickhouse", "SELECT sleepEachRow(1) FROM t"),
        ("clickhouse", "SELECT `sleep`(3)"),
        ("clickhouse", "SELECT 1 SETTINGS max_execution_time = 999"),
        ("clickhouse", "SELECT 1 FORMAT CSV"),
        ("clickhouse", "SYSTEM FLUSH LOGS"),
        ("clickhouse", "SELECT * FROM system.tables"),
        ("mssql", "SELECT * FROM t WITH (UPDLOCK)"),
        ("mssql", "SELECT * FROM t (XLOCK)"),
        ("mssql", "SELECT 1 WHILE 1 = 1 SELECT 2"),
        ("mssql", "SELECT 1 IF 1 = 1 SELECT 2"),
        ("mssql", "SELECT 1 PRINT 'x'"),
        ("clickhouse", "SELECT 1 # comment"),
        ("clickhouse", "SELECT $$text$$"),
        ("clickhouse", "SELECT 1 /* a /* b */"),
        ("clickhouse", "SELECT 1 INTO OUTFILE 'x'"),
        ("clickhouse", "OPTIMIZE TABLE t"),
        ("mssql", "EXEC sp_who"),
        ("mssql", "SELECT 1 EXEC('x')"),
        ("mssql", "SELECT 1 WAITFOR DELAY '00:00:05'"),
        ("mssql", "SELECT 1 DECLARE @x INT"),
        ("mssql", "SELECT * INTO t2 FROM t"),
        ("mssql", "SELECT 1 BEGIN TRANSACTION"),
        ("mssql", "SELECT 1 COMMIT"),
        ("mssql", "SELECT NEXT VALUE FOR s"),
        ("mssql", "SELECT * FROM OPENROWSET('x', 'y', 'z')"),
        ("mssql", "SELECT [xp_cmdshell]('dir')"),
        ("mssql", "SELECT 1 GO"),
        ("mssql", "SELECT 1 USE master"),
        ("mssql", "SELECT 1; DBCC CHECKDB"),
        ("mssql", "SELECT * FROM t WITH (NOLOCK) OPTION (USE HINT ('x'))"),
    ],
)
def test_new_dialects_refuse(dialect: str, sql: str) -> None:
    problem = sq.sql_statement_problem(sql, dialect)
    assert problem is not None and problem.startswith("QUERY_CONSTRUCT_NOT_ALLOWED:"), (
        problem
    )


def test_c6a_dialects_keep_their_rules() -> None:
    # ClickHouse-only words are not refused on PostgreSQL
    assert (
        sq.sql_statement_problem("SELECT settings, format FROM t", "postgresql") is None
    )
    assert sq.sql_statement_problem("SELECT 1 /* a /* b */ c */", "postgresql") is None


# -- sequences (fake connector) --------------------------------------------------


def test_clickhouse_sequence(fake) -> None:
    script = fake("clickhouse")
    outcome = _execute(_env(CH_URL), sq.QUERY_TOOL, {"sql": "SELECT 1 AS n"})
    assert outcome.status == "ok", _view(outcome)
    assert script.sent == [
        sq._CLICKHOUSE_GRANTS_CHECK,
        sq._CLICKHOUSE_READONLY_CHECK,
        "SELECT 1 AS n",
        "<close>",
    ]


@pytest.mark.parametrize(
    ("grants", "readonly", "code"),
    [
        (
            [("GRANT ALL ON *.* TO default WITH GRANT OPTION",)],
            "2",
            "SQL_ACCOUNT_NOT_READ_ONLY",
        ),
        ([("GRANT SELECT, INSERT ON shop.* TO ro",)], "2", "SQL_ACCOUNT_NOT_READ_ONLY"),
        ([("GRANT writer TO ro",)], "2", "SQL_ACCOUNT_NOT_READ_ONLY"),
        (
            [("GRANT SELECT ON shop.* TO ro",), ("REVOKE SELECT ON shop.x FROM ro",)],
            "2",
            "SQL_ACCOUNT_NOT_READ_ONLY",
        ),
        (CH_GRANTS, "1", "SQL_ACCOUNT_UNSUPPORTED"),
        (CH_GRANTS, "0", "SQL_ACCOUNT_NOT_READ_ONLY"),
    ],
)
def test_clickhouse_account_check(fake, grants, readonly, code) -> None:
    script = fake("clickhouse")
    script.grants = grants
    script.readonly = readonly
    outcome = _execute(_env(CH_URL), sq.QUERY_TOOL, {"sql": "SELECT 1"})
    assert outcome.reason == "SOURCE_FORBIDDEN", _view(outcome)
    assert code in str(_view(outcome))
    assert "SELECT 1" not in script.sent


def test_mssql_sequence_and_rollback(fake) -> None:
    script = fake("mssql")
    outcome = _execute(_env(MS_URL), sq.QUERY_TOOL, {"sql": "SELECT 1 AS n"})
    assert outcome.status == "ok", _view(outcome)
    sent = script.sent
    assert sent[0].startswith("SET LOCK_TIMEOUT ")
    assert sent[1:3] == ["SET XACT_ABORT ON", "SET IMPLICIT_TRANSACTIONS OFF"]
    assert sent[3].startswith(sq.ACCOUNT_CHECK_MARKER)
    assert sent[4:] == [
        "BEGIN TRANSACTION",
        "SELECT 1 AS n",
        "ROLLBACK TRANSACTION",
        "<close>",
    ]


def test_mssql_account_check_refuses(fake) -> None:
    script = fake("mssql")
    script.problems = [("database_role:db_datawriter",)]
    outcome = _execute(_env(MS_URL), sq.QUERY_TOOL, {"sql": "SELECT 1"})
    assert outcome.reason == "SOURCE_FORBIDDEN"
    assert "SQL_ACCOUNT_NOT_READ_ONLY" in str(_view(outcome))
    assert "BEGIN TRANSACTION" not in script.sent and "SELECT 1" not in script.sent


@pytest.mark.parametrize(
    ("number", "reason"),
    [
        (102, "INVALID_PARAMS"),
        (208, "INVALID_PARAMS"),
        (229, "SOURCE_FORBIDDEN"),
        (18456, "SOURCE_FORBIDDEN"),
        (1222, "TOOL_TIMEOUT"),
        (50000, "SOURCE_ERROR"),
    ],
)
def test_mssql_errors(fake, number, reason) -> None:
    import pytds

    script = fake("mssql")
    error = pytds.tds_base.OperationalError("m")
    error.number = number
    script.fail = error
    outcome = _execute(_env(MS_URL), sq.QUERY_TOOL, {"sql": "SELECT 1"})
    assert outcome.reason == reason, _view(outcome)
    assert script.sent[-2:] == ["ROLLBACK TRANSACTION", "<close>"]


# -- ClickHouse HTTP shape (real connection, local fake server) ------------------


class FakeClickHouse:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.answer: tuple[int, dict[str, str], bytes] = (200, {}, b"")

    def start(self) -> None:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                return None

            def _serve(self) -> None:
                parts = urlsplit(self.path)
                fake.requests.append(
                    {
                        "method": self.command,
                        "query": dict(parse_qsl(parts.query, keep_blank_values=True)),
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                    }
                )
                status, headers, body = fake.answer
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _serve

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.port = self._server.server_address[1]

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def clickhouse() -> Iterator[FakeClickHouse]:
    server = FakeClickHouse()
    server.start()
    yield server
    server.stop()


def _ch_config(port: int) -> sq.SqlConfig:
    return sq.SqlConfig(
        url=f"clickhouse+http://127.0.0.1:{port}/shop",
        username="ro",
        password=PASSWORD,
        tls="disable",
    )


def test_clickhouse_http_request_shape(clickhouse) -> None:
    lines = [
        json.dumps(["id", "id", "name"]),
        json.dumps([1, 2, "a"]),
        json.dumps([3, 4, "b"]),
    ]
    clickhouse.answer = (200, {}, ("\n".join(lines) + "\n").encode())
    conn = sq._ClickHouseConnection(_ch_config(clickhouse.port), 10.0)
    columns, rows, more = conn.query(
        "SELECT * FROM system.tables WHERE database = {p0:String}",
        ("shop",),
        max_rows=1,
    )
    assert columns == ["id", "id", "name"] and rows == [(1, 2, "a")] and more
    [request] = clickhouse.requests
    assert request["method"] == "GET"
    query = request["query"]
    assert query["database"] == "shop"
    assert query["default_format"] == "JSONCompactEachRowWithNames"
    assert query["timeout_overflow_mode"] == "throw"
    assert query["cancel_http_readonly_queries_on_client_close"] == "1"
    assert query["param_p0"] == "shop"
    assert query["query_id"].startswith("opspilot-")
    assert 1 <= int(query["max_execution_time"]) <= 8
    assert "readonly" not in query
    assert request["headers"]["x-clickhouse-user"] == "ro"
    assert request["headers"]["x-clickhouse-key"] == PASSWORD
    assert "authorization" not in request["headers"]


@pytest.mark.parametrize(
    ("status", "headers", "body", "kind"),
    [
        (
            400,
            {"X-ClickHouse-Exception-Code": "62"},
            b"Code: 62. DB::Exception: Syntax error",
            "invalid",
        ),
        (
            404,
            {"X-ClickHouse-Exception-Code": "60"},
            b"Code: 60. Unknown table",
            "invalid",
        ),
        (
            403,
            {"X-ClickHouse-Exception-Code": "497"},
            b"Code: 497. ACCESS_DENIED",
            "forbidden",
        ),
        (
            500,
            {"X-ClickHouse-Exception-Code": "164"},
            b"Code: 164. READONLY",
            "read_only",
        ),
        (
            500,
            {"X-ClickHouse-Exception-Code": "159"},
            b"Code: 159. TIMEOUT_EXCEEDED",
            "timeout",
        ),
        (500, {}, b"Code: 241. Memory limit", "unavailable"),
        (401, {}, b"", "forbidden"),
        (503, {}, b"", "unavailable"),
    ],
)
def test_clickhouse_errors(clickhouse, status, headers, body, kind) -> None:
    clickhouse.answer = (status, headers, body)
    conn = sq._ClickHouseConnection(_ch_config(clickhouse.port), 10.0)
    with pytest.raises(sq.SqlDriverError) as caught:
        conn.query("SELECT 1", max_rows=10)
    assert caught.value.kind == kind


def test_clickhouse_error_mid_stream(clickhouse) -> None:
    body = (
        json.dumps(["n"])
        + "\n"
        + json.dumps([1])
        + "\nCode: 159. DB::Exception: Timeout\n"
    )
    clickhouse.answer = (200, {}, body.encode())
    conn = sq._ClickHouseConnection(_ch_config(clickhouse.port), 10.0)
    with pytest.raises(sq.SqlDriverError) as caught:
        conn.query("SELECT 1", max_rows=10)
    assert caught.value.kind == "timeout"


def test_clickhouse_unreachable() -> None:
    conn = sq._ClickHouseConnection(_ch_config(9), 3.0)
    with pytest.raises(sq.SqlDriverError) as caught:
        conn.query("SELECT 1", max_rows=10)
    assert caught.value.kind == "unavailable"


# -- PR #749 review ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("mssql", "SELECT 1e1INSERT dbo.t VALUES (1)"),
        ("mssql", "SELECT 1E1TRUNCATE TABLE dbo.t"),
        ("mssql", "SELECT 0x0WAITFOR DELAY '00:00:10'"),
        ("mssql", "SELECT 1.e1KILL 55"),
        ("mssql", "SELECT 1e1EXEC master..xp_cmdshell 'whoami'"),
        ("mssql", "SELECT 1e1EXEC msdb.dbo.sp_send_dbmail @recipients='a@b'"),
        ("mssql", "SELECT 1 --\rINSERT dbo.t VALUES(1)"),
        ("postgresql", "SELECT 1 --\r; DELETE FROM t"),
        ("mysql", "SELECT 1 -- \r; DELETE FROM t"),
        ("clickhouse", "SELECT 1 --\rINSERT INTO t VALUES (1)"),
        ("clickhouse", "SELECT 1e1INSERT"),
        ("postgresql", "SELECT 1e1DELETE"),
        ("mssql", "SELECT * FROM master.dbo.sysobjects"),
        ("mssql", "SELECT * FROM [master].[dbo].[t]"),
        ("mssql", "SELECT * FROM master..t"),
        ("mssql", "SELECT * FROM srv.db.dbo.t"),
        ("mssql", "SELECT sp_who FROM t"),
        ("mssql", "SELECT [xp_cmdshell] FROM t"),
        ("mssql", "SELECT 1 RECEIVE"),
        ("mssql", "SELECT 1 SEND"),
        ("mssql", "SELECT 1 ADD SIGNATURE"),
        ("mssql", "SELECT 1 RETURN"),
        ("mssql", "SELECT 1 OPEN c"),
        ("mssql", "SELECT 1 FETCH NEXT FROM c"),
        ("mssql", "SELECT * FROM t WITH (READCOMMITTEDLOCK)"),
        ("mssql", "SELECT * FROM t WITH (NOWAIT)"),
        ("mssql", "SELECT $1e1EXEC x"),
        ("mssql", "SELECT $1.e1EXEC x"),
        ("clickhouse", "SELECT * FROM cosn('x')"),
        ("clickhouse", "SELECT * FROM hive('thrift://x', 'd', 't', 'a String', 'a')"),
        ("clickhouse", "SELECT * FROM deltaLakeS3('x')"),
        ("clickhouse", "SELECT * FROM paimon('x')"),
        ("clickhouse", "SELECT * FROM icebergCluster('c', 'x')"),
    ],
)
def test_review_cases_refused(dialect: str, sql: str) -> None:
    problem = sq.sql_statement_problem(sql, dialect)
    assert problem is not None and problem.startswith("QUERY_CONSTRUCT_NOT_ALLOWED:"), (
        problem
    )


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("mssql", "SELECT 1.5, 2e3, 0x1F, .5 FROM t"),
        ("mssql", "SELECT $1.50 AS price, $.5, a$1 FROM t"),
        ("mssql", "SELECT o.name FROM sys.objects o"),
        ("mssql", "SELECT CASE WHEN a = 1 THEN 'x' END FROM t"),
        ("mssql", "SELECT * FROM t ORDER BY id OFFSET 0 ROWS FETCH NEXT 5 ROWS ONLY"),
        ("postgresql", "SELECT 1.5e-3, t1.a FROM t1"),
        ("clickhouse", "SELECT 1e10, 0xFF"),
        ("mysql", "SELECT a FROM t2 WHERE b = 3"),
    ],
)
def test_review_cases_allowed(dialect: str, sql: str) -> None:
    assert sq.sql_statement_problem(sql, dialect) is None
