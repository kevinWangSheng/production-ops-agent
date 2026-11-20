"""Implementer unit tests for the SQL tools (C6a, #570).

The independent contract tests are the acceptance suite; these pin the
implementer's own reversible choices (C6 record, C6a interface) through the
public profile path with a recording fake connector. No database, no model.
Synthetic credentials are low-entropy placeholders.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

import pytest

from opspilot.tools import ToolRequest
from opspilot.tools import otel_demo_ext as ext
from opspilot.tools import sql_query as sq
from opspilot.tools.profiles import select_profile
from opspilot.tools.registry import ToolContractError
from opspilot.tracing import check_lab_target
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import NOW, TARGET_ID, FakeStore, _lease
from tests.test_tool_expansion_c5a_contract import FRAME, _base_env

PASSWORD = "readonly" + "570"
PG_URL = "postgresql://127.0.0.1:55432/shop"
MY_URL = "mysql://127.0.0.1:53306/shop"

READ_ONLY_GRANTS = [
    ("GRANT PROCESS ON *.* TO `ro`@`%`",),
    ("GRANT SELECT, SHOW VIEW ON `shop`.* TO `ro`@`%`",),
    ("GRANT SELECT ON `performance_schema`.* TO `ro`@`%`",),
]


class FakeConnection:
    def __init__(self, script: FakeScript) -> None:
        self.script = script
        self.server_version = script.version

    def execute(self, statement: str, params: Sequence[object] = ()) -> None:
        self.script.sent.append(statement)
        if self.script.fail_on and self.script.fail_on(statement):
            raise self.script.error

    def query(
        self, statement: str, params: Sequence[object] = (), *, max_rows: int
    ) -> tuple[list[str], list[tuple[Any, ...]], bool]:
        self.script.sent.append(statement)
        if self.script.fail_on and self.script.fail_on(statement):
            raise self.script.error
        if statement.startswith(sq.ACCOUNT_CHECK_MARKER):
            return ["problem"], list(self.script.account_rows), False
        if statement.startswith("SELECT VERSION()"):
            return ["v", "id"], [(self.script.version, 7)], False
        columns, rows = self.script.answer(statement)
        return columns, rows[:max_rows], len(rows) > max_rows

    def cancel(self) -> None:
        self.script.sent.append("<cancel>")

    def close(self) -> None:
        self.script.sent.append("<close>")


class FakeScript:
    def __init__(self, dialect: str) -> None:
        self.dialect = dialect
        self.version = "17.2" if dialect == "postgresql" else "8.0.40"
        self.account_rows: list[tuple[Any, ...]] = (
            [] if dialect == "postgresql" else list(READ_ONLY_GRANTS)
        )
        self.sent: list[str] = []
        self.connects = 0
        self.columns: list[str] = ["id", "name"]
        self.rows: list[tuple[Any, ...]] = [(1, "a"), (2, "b")]
        self.fail_on: Callable[[str], bool] | None = None
        self.error: Exception = sq.SqlDriverError("error")
        self.answers: dict[str, tuple[list[str], list[tuple[Any, ...]]]] = {}

    def answer(self, statement: str) -> tuple[list[str], list[tuple[Any, ...]]]:
        for key, value in self.answers.items():
            if key in statement:
                return value
        return self.columns, self.rows

    def connector(self, config: sq.SqlConfig, timeout: float) -> FakeConnection:
        self.connects += 1
        assert config.password == PASSWORD
        return FakeConnection(self)


@pytest.fixture
def fake_db(monkeypatch, tmp_path) -> Callable[[str], FakeScript]:
    ca = tmp_path / "ca.pem"
    ca.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("C5A_TEST_K8S_CA_FILE", str(ca))

    def make(dialect: str) -> FakeScript:
        script = FakeScript(dialect)
        monkeypatch.setattr(sq, "default_connector", script.connector)
        return script

    return make


def _env(url: str = PG_URL, **extra: str) -> dict[str, str]:
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
    executor = factory(lease, model_input)
    return executor.execute(
        ToolRequest(
            step_id="c6a-step",
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


def _model_statements(script: FakeScript) -> list[str]:
    return [
        s
        for s in script.sent
        if not s.startswith(("<", sq.ACCOUNT_CHECK_MARKER, "SET ", "BEGIN", "START"))
        and s not in ("ROLLBACK", "SELECT VERSION(), CONNECTION_ID()")
    ]


# -- statement check (C6-3) --------------------------------------------------


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("postgresql", "SELECT 1"),
        ("postgresql", "select * from t where c = 'DELETE' limit 5;"),
        ("postgresql", 'SELECT "lock", "update" FROM t'),
        ("postgresql", "SELECT $$ drop $$, $x$ insert $x$"),
        ("postgresql", "SELECT E'a\\'b'"),
        ("postgresql", "EXPLAIN ANALYZE SELECT * FROM t"),
        ("postgresql", "/* a /* nested */ b */ SELECT 1"),
        ("postgresql", "( (SELECT 1) ) UNION (SELECT 2)"),
        ("postgresql", "SHOW work_mem"),
        ("postgresql", "SELECT lower(name), log(2), a$b FROM t"),
        ("mysql", "SELECT 1 -- comment\n"),
        ("mysql", "SELECT 1 # DELETE"),
        ("mysql", "SELECT `update` FROM t"),
        ("mysql", "DESC t"),
        ("mysql", "SHOW PROCESSLIST"),
    ],
)
def test_statement_check_allows(dialect: str, sql: str) -> None:
    assert sq.sql_statement_problem(sql, dialect) is None


@pytest.mark.parametrize(
    ("dialect", "sql", "code"),
    [
        ("postgresql", "", "INVALID_QUERY"),
        ("postgresql", "SELECT 'x", "INVALID_QUERY"),
        ("postgresql", "SELECT 1\x00", "INVALID_QUERY"),
        ("postgresql", "SELECT " + "1," * 10_001 + "1", "INVALID_QUERY"),
        ("postgresql", "SELECT 1; SELECT 2", "QUERY_CONSTRUCT_NOT_ALLOWED"),
        ("postgresql", "SELECT 1;;", "QUERY_CONSTRUCT_NOT_ALLOWED"),
        ("postgresql", "DELETE FROM t", "QUERY_CONSTRUCT_NOT_ALLOWED"),
        ("postgresql", "VALUES (1)", "QUERY_CONSTRUCT_NOT_ALLOWED"),
        (
            "postgresql",
            "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
            "QUERY_CONSTRUCT_NOT_ALLOWED",
        ),
        ("postgresql", "SELECT * INTO n FROM t", "QUERY_CONSTRUCT_NOT_ALLOWED"),
        (
            "postgresql",
            "SELECT * FROM t FOR NO KEY UPDATE",
            "QUERY_CONSTRUCT_NOT_ALLOWED",
        ),
        ("postgresql", "SELECT * FROM t FOR KEY SHARE", "QUERY_CONSTRUCT_NOT_ALLOWED"),
        ("postgresql", "SELECT pg_sleep(1)", "QUERY_CONSTRUCT_NOT_ALLOWED"),
        ("postgresql", "SELECT pg_sleep_for('1s')", "QUERY_CONSTRUCT_NOT_ALLOWED"),
        (
            "postgresql",
            "SELECT pg_catalog.pg_sleep /**/ (1)",
            "QUERY_CONSTRUCT_NOT_ALLOWED",
        ),
        ("postgresql", 'SELECT "pg_sleep"(1)', "QUERY_CONSTRUCT_NOT_ALLOWED"),
        ("postgresql", 'SELECT U&"pg_sleep"(1)', "QUERY_CONSTRUCT_NOT_ALLOWED"),
        ("postgresql", "SELECT lo_export(1, '/tmp/x')", "QUERY_CONSTRUCT_NOT_ALLOWED"),
        ("postgresql", "SELECT dblink_exec('x')", "QUERY_CONSTRUCT_NOT_ALLOWED"),
        (
            "postgresql",
            "SELECT set_config('a', 'b', false)",
            "QUERY_CONSTRUCT_NOT_ALLOWED",
        ),
        # a backslash read both ways: the second reading ends the literal
        (
            "postgresql",
            "SELECT 'a\\'; DELETE FROM t; --'",
            "QUERY_CONSTRUCT_NOT_ALLOWED",
        ),
        ("mysql", "SELECT 1 --x", "QUERY_CONSTRUCT_NOT_ALLOWED"),
        ("mysql", "SELECT /*! DELETE */ 1", "QUERY_CONSTRUCT_NOT_ALLOWED"),
        ("mysql", "SELECT /*M! DELETE */ 1", "QUERY_CONSTRUCT_NOT_ALLOWED"),
        ("mysql", "SELECT SLEEP(1)", "QUERY_CONSTRUCT_NOT_ALLOWED"),
        ("mysql", "SELECT * FROM t LOCK IN SHARE MODE", "QUERY_CONSTRUCT_NOT_ALLOWED"),
        ("mysql", "SELECT replace(a, 'x', 'y') FROM t", "QUERY_CONSTRUCT_NOT_ALLOWED"),
        (
            "mysql",
            "SELECT * FROM t INTO OUTFILE '/tmp/x'",
            "QUERY_CONSTRUCT_NOT_ALLOWED",
        ),
        ("mysql", "SET @a = 1", "QUERY_CONSTRUCT_NOT_ALLOWED"),
        ("mysql", "SELECT `sleep`(1)", "QUERY_CONSTRUCT_NOT_ALLOWED"),
    ],
)
def test_statement_check_refuses(dialect: str, sql: str, code: str) -> None:
    problem = sq.sql_statement_problem(sql, dialect)
    assert problem is not None and problem.startswith(code + ":"), problem


# -- the session (C6-4, C6-U1, C6-8) ----------------------------------------


def test_postgres_sequence_and_rows(fake_db) -> None:
    script = fake_db("postgresql")
    script.columns = ["id", "id", "note"]
    script.rows = [(1, 2, "x" * 5000), (3, 4, b"\x01\x02")]
    outcome = _execute(_env(), sq.QUERY_TOOL, {"sql": "SELECT id, id, note FROM t"})
    assert outcome.status == "ok", _view(outcome)
    sent = script.sent
    assert sent[0] == "BEGIN READ ONLY"
    assert sent[1].startswith("SET LOCAL statement_timeout = ")
    assert sent[2].startswith("SET LOCAL lock_timeout = ")
    assert sent[3].startswith("SET LOCAL idle_in_transaction_session_timeout = ")
    assert sent[4].startswith(sq.ACCOUNT_CHECK_MARKER)
    assert sent[5] == "SELECT id, id, note FROM t"
    assert sent[6:] == ["ROLLBACK", "<close>"]
    ms = int(sent[1].rsplit(" ", 1)[1])
    assert 25_000 <= ms <= 28_000
    view = _view(outcome)
    rows = view["content"]
    assert rows[0]["id"] == 1 and rows[0]["id#2"] == 2
    assert len(rows[0]["note"]) == sq.MAX_VALUE_CHARS
    assert rows[0]["_truncated_fields"] == ["note"]
    assert rows[1]["note"] == "0x0102"
    assert view["columns"] == ["id", "id#2", "note"]
    assert view["long_values_cut"] == 1
    assert view["dialect"] == "postgresql"


def test_mysql_and_mariadb_sequences(fake_db) -> None:
    script = fake_db("mysql")
    outcome = _execute(_env(MY_URL), sq.QUERY_TOOL, {"sql": "SELECT 1"})
    assert outcome.status == "ok", _view(outcome)
    assert script.sent[0] == "SELECT VERSION(), CONNECTION_ID()"
    assert script.sent[1].startswith("SET SESSION max_execution_time = ")
    assert script.sent[2].startswith("SET SESSION lock_wait_timeout = ")
    assert script.sent[3] == sq.ACCOUNT_CHECK_MARKER + " SHOW GRANTS"
    assert script.sent[4] == "START TRANSACTION READ ONLY"
    assert script.sent[5:] == ["SELECT 1", "ROLLBACK", "<close>"]
    script = fake_db("mysql")
    script.version = "11.6.2-MariaDB-ubu2404"
    _execute(_env("mariadb://127.0.0.1:53307/shop"), sq.QUERY_TOOL, {"sql": "SELECT 1"})
    assert script.sent[1].startswith("SET SESSION max_statement_time = ")


def test_limit_marks_incomplete(fake_db) -> None:
    script = fake_db("postgresql")
    script.rows = [(i, str(i)) for i in range(600)]
    outcome = _execute(_env(), sq.QUERY_TOOL, {"sql": "SELECT * FROM t", "limit": 50})
    view = _view(outcome)
    assert outcome.status == "ok", view
    assert len(view["content"]) == 50
    assert view["incomplete_reason"]
    assert view["limit"] == 50


@pytest.mark.parametrize(
    ("rows", "fragment"),
    [
        ([("role_attribute:superuser",)], "SQL_ACCOUNT_NOT_READ_ONLY"),
        ([("predefined_role:pg_write_all_data",)], "SQL_ACCOUNT_NOT_READ_ONLY"),
        ([("relation_write",), ("opspilot_schema",)], "SQL_TARGETS_OPSPILOT_DB"),
    ],
)
def test_postgres_account_check_refuses_before_the_model_statement(
    fake_db, rows, fragment
) -> None:
    script = fake_db("postgresql")
    script.account_rows = rows
    outcome = _execute(_env(), sq.QUERY_TOOL, {"sql": "SELECT 1"})
    assert outcome.status == "error" and outcome.reason == "SOURCE_FORBIDDEN"
    assert fragment in str(_view(outcome))
    assert _model_statements(script) == []
    assert script.sent[-2:] == ["ROLLBACK", "<close>"]


@pytest.mark.parametrize(
    "grant",
    [
        "GRANT ALL PRIVILEGES ON `shop`.* TO `u`@`%`",
        "GRANT SELECT, INSERT ON `shop`.* TO `u`@`%`",
        "GRANT SELECT ON *.* TO `u`@`%` WITH GRANT OPTION",
        "GRANT `writer`@`%` TO `u`@`%`",
        "GRANT PROXY ON ''@'' TO 'root'@'%' WITH GRANT OPTION",
        "GRANT CREATE TEMPORARY TABLES ON `shop`.* TO `u`@`%`",
        "REVOKE INSERT ON `shop`.* FROM `u`@`%`",
    ],
)
def test_mysql_grants_refuse(fake_db, grant) -> None:
    script = fake_db("mysql")
    script.account_rows = [*READ_ONLY_GRANTS, (grant,)]
    outcome = _execute(_env(MY_URL), sq.QUERY_TOOL, {"sql": "SELECT 1"})
    assert outcome.status == "error" and outcome.reason == "SOURCE_FORBIDDEN"
    assert "SQL_ACCOUNT_NOT_READ_ONLY" in str(_view(outcome))
    assert "START TRANSACTION READ ONLY" not in script.sent
    assert _model_statements(script) == []


def test_mysql_grants_allow_read_only_forms() -> None:
    rows = [
        *READ_ONLY_GRANTS,
        ("GRANT USAGE ON *.* TO `u`@`%` IDENTIFIED BY PASSWORD '*AB'",),
        ("GRANT SELECT (`a`, `b`) ON `shop`.`t` TO `u`@`%`",),
        ("GRANT REPLICATION CLIENT,PROCESS ON *.* TO `u`@`%`",),
    ]
    assert sq.session_problem("mysql", rows) is None
    problem = sq.session_problem(
        "mysql",
        [
            ("GRANT USAGE ON *.* TO `u`@`%` IDENTIFIED BY PASSWORD '*AB'",),
            ("GRANT INSERT ON `shop`.* TO `u`@`%` IDENTIFIED BY PASSWORD '*AB'",),
        ],
    )
    assert problem is not None and "*AB" not in problem


def test_refusal_before_connect(fake_db) -> None:
    script = fake_db("postgresql")
    for params in (
        {"sql": "DELETE FROM t"},
        {"sql": "SELECT 1", "limit": 0},
        {"sql": "SELECT 1", "limit": 501},
    ):
        outcome = _execute(_env(), sq.QUERY_TOOL, params)
        assert outcome.reason == "INVALID_PARAMS", _view(outcome)
    outcome = _execute(_env(), sq.DESCRIBE_TOOL, {"table_name": "a b"})
    assert outcome.reason == "INVALID_PARAMS"
    assert "INVALID_IDENTIFIER" in str(_view(outcome))
    assert script.connects == 0


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("invalid", "INVALID_PARAMS"),
        ("forbidden", "SOURCE_FORBIDDEN"),
        ("read_only", "SOURCE_FORBIDDEN"),
        ("unavailable", "SOURCE_UNAVAILABLE"),
        ("error", "SOURCE_ERROR"),
        ("timeout", "TOOL_TIMEOUT"),
    ],
)
def test_driver_errors(fake_db, kind, reason) -> None:
    script = fake_db("postgresql")
    script.fail_on = lambda statement: statement == "SELECT 1"
    script.error = sq.SqlDriverError(kind, "42P01", 'relation "t" does not exist')
    outcome = _execute(_env(), sq.QUERY_TOOL, {"sql": "SELECT 1"})
    assert outcome.reason == reason, _view(outcome)
    assert script.sent[-2:] == ["ROLLBACK", "<close>"]
    if kind == "invalid":
        assert 'relation "t" does not exist' in str(_view(outcome))
    else:
        assert "does not exist" not in str(_view(outcome))


def test_list_and_describe(fake_db) -> None:
    script = fake_db("postgresql")
    script.answers = {
        "pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind::text": (
            ["s", "n", "k"],
            [("public", "orders", "r"), ("public", "v_orders", "v")],
        ),
    }
    outcome = _execute(_env(), sq.LIST_TOOL, {})
    assert outcome.status == "ok", _view(outcome)
    assert _view(outcome)["content"] == [
        {"schema": "public", "name": "orders", "type": "table"},
        {"schema": "public", "name": "v_orders", "type": "view"},
    ]
    assert all(s.startswith(sq.CATALOG_MARKER) for s in _model_statements(script))
    script = fake_db("postgresql")
    script.answers = {
        "pg_table_is_visible": (["oid", "s", "t"], [(42, "public", "orders")]),
        "pg_attribute a LEFT JOIN": (
            ["n", "t", "null", "d", "o"],
            [("id", "integer", False, None, 1), ("password", "text", True, None, 2)],
        ),
        "pg_constraint con": (
            ["n", "k", "c", "rs", "rt", "rc"],
            [("orders_pkey", "p", ["id"], None, None, [])],
        ),
        "pg_index i": (
            ["n", "u", "p", "c", "d"],
            [("orders_pkey", True, True, ["id"], "CREATE UNIQUE INDEX ...")],
        ),
    }
    outcome = _execute(_env(), sq.DESCRIBE_TOOL, {"table_name": "orders"})
    view = _view(outcome)
    assert outcome.status == "ok", view
    assert [c["name"] for c in view["content"]] == ["id", "password"]
    assert view["primary_key"] == ["id"]
    assert view["indexes"][0]["unique"] is True
    script = fake_db("postgresql")
    script.answers = {"pg_table_is_visible": (["oid", "s", "t"], [])}
    outcome = _execute(_env(), sq.DESCRIBE_TOOL, {"table_name": "missing"})
    assert outcome.reason == "INVALID_PARAMS"
    assert "TABLE_NOT_FOUND" in str(_view(outcome))


def test_password_column_is_redacted(fake_db) -> None:
    script = fake_db("postgresql")
    script.columns = ["user_name", "password"]
    script.rows = [("alice", "hunter2-plain-value")]
    outcome = _execute(_env(), sq.QUERY_TOOL, {"sql": "SELECT * FROM users"})
    assert outcome.status == "ok", _view(outcome)
    assert "hunter2-plain-value" not in str(_view(outcome))


# -- configuration, lab gate, profile (C6-7, C6-8, C6-9, C6-13) --------------


@pytest.mark.parametrize(
    ("env", "code"),
    [
        ({sq.URL_ENV: PG_URL}, "SQL_CONFIG_MISSING"),
        ({sq.URL_ENV: PG_URL, sq.USERNAME_ENV: "ro"}, "SQL_CONFIG_MISSING"),
        ({sq.USERNAME_ENV: "ro"}, "SQL_CONFIG_INVALID"),
        (
            {
                sq.URL_ENV: "postgresql://ro:x@127.0.0.1/shop",
                sq.USERNAME_ENV: "ro",
                sq.PASSWORD_ENV: "x",
            },
            "SQL_CONFIG_INVALID",
        ),
        (
            {
                sq.URL_ENV: "postgresql://127.0.0.1/shop?sslmode=disable",
                sq.USERNAME_ENV: "ro",
                sq.PASSWORD_ENV: "x",
            },
            "SQL_CONFIG_INVALID",
        ),
        (
            {
                sq.URL_ENV: "postgresql://127.0.0.1/",
                sq.USERNAME_ENV: "ro",
                sq.PASSWORD_ENV: "x",
            },
            "SQL_CONFIG_INVALID",
        ),
        (
            {
                sq.URL_ENV: "mssql://127.0.0.1/shop",
                sq.USERNAME_ENV: "ro",
                sq.PASSWORD_ENV: "x",
            },
            "SQL_CONFIG_INVALID",
        ),
        (
            {
                sq.URL_ENV: PG_URL,
                sq.USERNAME_ENV: "ro",
                sq.PASSWORD_ENV: "x",
                sq.TLS_ENV: "prefer",
            },
            "SQL_CONFIG_INVALID",
        ),
        (
            {
                sq.URL_ENV: PG_URL,
                sq.USERNAME_ENV: "ro",
                sq.PASSWORD_ENV: "x",
                sq.TLS_ENV: "disable",
                sq.CA_FILE_ENV: "/etc/hosts",
            },
            "SQL_CONFIG_INVALID",
        ),
    ],
)
def test_config_errors(env, code) -> None:
    with pytest.raises(ToolContractError, match=code):
        sq.SqlConfig.from_env(env)


@pytest.mark.parametrize(
    ("dsn_env", "dsn"),
    [
        ("OPSPILOT_WORKER_DSN", "postgresql://w:x@localhost:5432/opspilot"),
        ("OPSPILOT_DSN", "host=127.0.0.1 port=5432 dbname=opspilot"),
        ("OPSPILOT_LAB_DSN", "postgresql://w:x@[::1]/opspilot"),
    ],
)
def test_config_refuses_opspilot_database(dsn_env, dsn) -> None:
    env = {
        sq.URL_ENV: "postgresql://127.0.0.1:5432/shop",
        sq.USERNAME_ENV: "ro",
        sq.PASSWORD_ENV: "x",
        dsn_env: dsn,
    }
    with pytest.raises(ToolContractError, match="SQL_TARGETS_OPSPILOT_DB"):
        sq.SqlConfig.from_env(env)
    env[sq.URL_ENV] = "postgresql://127.0.0.1:10217/shop"
    assert sq.SqlConfig.from_env(env) is not None


def test_profile_and_lab_gate(fake_db) -> None:
    assert (
        ext.tool_schema_revision()
        == ext.TOOL_SCHEMA_REVISION
        == "otel-demo-ext-c63f058afb18"
    )
    assert ext.SQL_POSTGRESQL_TOOL_SCHEMA_REVISION != ext.SQL_MYSQL_TOOL_SCHEMA_REVISION
    assert sq.sql_dialect({}) is None
    assert sq.sql_dialect({sq.URL_ENV: "mariadb://h/db"}) == "mysql"
    with pytest.raises(SystemExit, match="SQL_CONFIG_INVALID"):
        sq.sql_dialect({sq.URL_ENV: "clickhouse+http://h/db"})
    profile = select_profile(_env())
    assert (
        profile.versions()["tool_schema_revision"]
        == ext.SQL_POSTGRESQL_TOOL_SCHEMA_REVISION
    )
    names = [
        s["function"]["name"] for s in profile.face(FakeClock(start=NOW)).tool_schemas
    ]
    assert names[-3:] == list(sq.TOOL_NAMES)
    assert "SQL_NOT_LAB" in check_lab_target(
        {sq.URL_ENV: "postgresql://db.example.com/x"}
    )
    assert "SQL_NOT_LAB" not in check_lab_target({sq.URL_ENV: PG_URL})
    with pytest.raises(SystemExit, match="SQL_CONFIG_MISSING"):
        env = _env()
        del env[sq.PASSWORD_ENV]
        select_profile(env).executor_factory(
            None, RecordingSink(), FakeClock(start=NOW)
        )  # type: ignore[arg-type]
