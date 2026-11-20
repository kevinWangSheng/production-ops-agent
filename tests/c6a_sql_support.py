"""Shared fakes for the C6a contract tests (#570): a recording stand-in for
the SQL driver seam and the worker built around it.

Written from the C6 task record (docs/tasks/2026-11-20-tool-expansion-c6.md,
「行为（公开接口，C6a）」), without reading the implementation. The seam is
the one the record names: ``SqlTransport(config, *, connector=None,
request_timeout_seconds=30.0)`` with ``connector(config, timeout_seconds) ->
SqlConnection`` and ``SqlConnection.execute(statement, params=())``,
``.query(statement, params=(), max_rows) -> (columns, rows, more)``,
``.server_version``, ``.cancel()``, ``.close()``.

Seams assumed beyond the record (each also marked ``# ASSUMPTION:``):

* The module is ``opspilot.tools.sql_query`` and the worker constructs its
  transport by calling ``SqlTransport(...)`` (looked up when the executor
  factory is built), so a subclass patched over every module attribute that
  names the original class receives the fake connector.
* ``query`` returns ``columns`` as a sequence of column-name strings and
  ``rows`` as a sequence of tuples; ``max_rows`` may be passed by keyword.
* The PostgreSQL account check answers one problem code per row in its first
  column; MySQL/MariaDB ``SHOW GRANTS`` answers one grant line per row.

All synthetic credentials are low-entropy placeholders.
"""

from __future__ import annotations

import itertools
import os
import re
import shutil
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest

from opspilot.tools import ToolRequest
from opspilot.tools.outcomes import Window
from opspilot.tools.profiles import PROFILE_ENV, ToolProfile, select_profile
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import NOW, TARGET_ID, FakeStore, _lease

EXT_PROFILE = "otel-demo-ext"
QUERY = "database_sql_query"
LIST_TABLES = "database_sql_list_tables"
DESCRIBE = "database_sql_describe_table"
SQL_TOOLS = (QUERY, LIST_TABLES, DESCRIBE)

ACCOUNT_MARK = "/* opspilot:account-check */"
CATALOG_MARK = "/* opspilot:catalog */"

# Nothing listens on these ports: a transport that ignored the fake connector
# would fail as a source outage, never pass.
PG_URL = "postgresql://127.0.0.1:15432/c6tdb"
MYSQL_URL = "mysql://127.0.0.1:13306/c6tdb"
MARIADB_URL = "mariadb://127.0.0.1:13307/c6tdb"

SQL_USERNAME = "c6t-reader"
SQL_PASSWORD = "c6t-pw-" + "aaaa570"
K8S_TOKEN = "eeeeeeee570"
K8S_API = "https://127.0.0.1:16443"
PROM_USERNAME = "investigator"
PROM_PASSWORD = "dddddddd570"
CANARY = "ffffffff570"

MYSQL_VERSION = "8.0.40"
MARIADB_VERSION = "11.6.2-MariaDB-ubu2404"
CONNECTION_ID = 4242

#: The C6-21 read-only account's SHOW GRANTS on MySQL.
GOOD_GRANTS = (
    "GRANT PROCESS ON *.* TO `c6t_ro`@`%`",
    "GRANT SELECT, SHOW VIEW ON `c6tdb`.* TO `c6t_ro`@`%`",
    "GRANT SELECT ON `performance_schema`.* TO `c6t_ro`@`%`",
)

SQL_ENV_NAMES = (
    "OPSPILOT_SQL_URL",
    "OPSPILOT_SQL_USERNAME",
    "OPSPILOT_SQL_PASSWORD",
    "OPSPILOT_SQL_TLS",
    "OPSPILOT_SQL_CA_FILE",
)

FRAME = Window(NOW - timedelta(hours=6), NOW)
_INDEX = itertools.count()


# -- the recording driver -------------------------------------------------------


@dataclass
class Sent:
    method: str  # "execute" or "query"
    statement: str
    params: tuple[Any, ...]
    max_rows: int | None
    at: float


def _norm(statement: str) -> str:
    return re.sub(r"\s+", " ", statement).strip().upper()


_BEGIN = re.compile(
    r"^(BEGIN|START TRANSACTION)( TRANSACTION| WORK)?"
    r"( ISOLATION LEVEL [A-Z ]+?,?)? READ ONLY;?$"
)


def kind(statement: str, model: str | None = None) -> str:
    """A coarse label of one statement the tool sent."""
    if model is not None and statement == model:
        return "model"
    if "opspilot:account-check" in statement:
        return "check"
    if "opspilot:catalog" in statement:
        return "catalog"
    text = _norm(statement)
    if _BEGIN.match(text):
        return "begin"
    if re.match(r"^ROLLBACK( WORK)?;?$", text):
        return "rollback"
    match = re.match(r"^SET (LOCAL|SESSION) (@@)?([A-Z_]+)", text)
    if match:
        return f"set:{match.group(3).lower()}"
    if "VERSION()" in text and "CONNECTION_ID()" in text:
        return "version"
    if text.startswith("KILL"):
        return "kill"
    return "other"


class FakeConnection:
    def __init__(self, driver: FakeSql, config: Any, timeout_seconds: float):
        self.driver = driver
        self.config = config
        self.timeout_seconds = timeout_seconds
        # The model statement of the call that opened this connection.
        self.model = driver.model
        self.sent: list[Sent] = []
        self.cancelled = threading.Event()
        self.cancel_calls = 0
        self.closed = 0

    @property
    def server_version(self) -> str:
        return self.driver.version

    def _record(self, method, statement, params, max_rows) -> Sent:
        item = Sent(method, statement, tuple(params or ()), max_rows, time.monotonic())
        with self.driver.lock:
            self.sent.append(item)
        return item

    def execute(self, statement: str, params: Sequence[Any] = ()) -> None:
        self._record("execute", statement, params, None)
        self.driver.raise_for(kind(statement, self.model), self)
        if kind(statement) == "kill":
            for other in self.driver.connections:
                if other is not self:
                    other.cancelled.set()

    def query(
        self,
        statement: str,
        params: Sequence[Any] = (),
        max_rows: int | None = None,
        **extra: Any,
    ):
        if max_rows is None:
            max_rows = extra.get("max_rows")
        self._record("query", statement, params, max_rows)
        label = kind(statement, self.model)
        self.driver.raise_for(label, self)
        if label == "version":
            return (
                ["VERSION()", "CONNECTION_ID()"],
                [(self.driver.version, CONNECTION_ID)],
                False,
            )
        if label == "check":
            if self.driver.dialect == "postgresql":
                return ["problem"], [(code,) for code in self.driver.problems], False
            return (
                [f"Grants for {SQL_USERNAME}@%"],
                [(line,) for line in self.driver.grants],
                False,
            )
        if label == "catalog":
            columns, rows = self.driver.catalog
            return list(columns), list(rows), False
        if label == "model" and self.driver.block_seconds:
            self._block()
        columns, rows = self.driver.result
        rows = list(rows)
        if max_rows is not None and len(rows) > max_rows:
            return list(columns), rows[:max_rows], True
        return list(columns), rows, False

    def _block(self) -> None:
        """A long query: ends when cancelled, or -- MySQL -- when the
        driver's read timeout (the connect ``timeout_seconds``) passes."""
        limit = self.driver.block_seconds
        if self.driver.dialect == "mysql":
            limit = min(limit, self.timeout_seconds)
        cancelled = self.cancelled.wait(limit)
        if self.driver.dialect == "postgresql":
            import psycopg.errors

            raise psycopg.errors.QueryCanceled(
                "canceling statement due to user request"
            )
        pymysql = pytest.importorskip("pymysql")
        if cancelled:
            raise pymysql.err.OperationalError(1317, "Query execution was interrupted")
        raise pymysql.err.OperationalError(
            2013, "Lost connection to MySQL server during query (timed out)"
        )

    def cancel(self) -> None:
        self.cancel_calls += 1
        self.cancelled.set()

    def close(self) -> None:
        self.closed += 1


class FakeSql:
    """The connector: one ``FakeConnection`` per call, every statement kept."""

    def __init__(self, dialect: str, version: str | None = None) -> None:
        assert dialect in ("postgresql", "mysql")
        self.dialect = dialect
        self.version = version or ("17.2" if dialect == "postgresql" else MYSQL_VERSION)
        self.connections: list[FakeConnection] = []
        self.lock = threading.Lock()
        self.problems: list[str] = []  # PostgreSQL account-check rows
        self.grants: list[str] = list(GOOD_GRANTS)  # MySQL SHOW GRANTS rows
        self.catalog: tuple[Sequence[str], Sequence[tuple]] = ([], [])
        self.result: tuple[Sequence[str], Sequence[tuple]] = (["n"], [(1,)])
        self.model: str | None = None
        self.errors: dict[str, BaseException] = {}  # kind -> raised on it
        self.connect_error: BaseException | None = None
        self.block_seconds = 0.0
        self.configs: list[Any] = []
        self.timeouts: list[float] = []

    def connect(self, config: Any, timeout_seconds: float) -> FakeConnection:
        with self.lock:
            self.configs.append(config)
            self.timeouts.append(timeout_seconds)
        if self.connect_error is not None:
            raise self.connect_error
        connection = FakeConnection(self, config, timeout_seconds)
        with self.lock:
            self.connections.append(connection)
        return connection

    def raise_for(self, label: str, connection: FakeConnection) -> None:
        error = self.errors.get(label)
        if error is not None:
            raise error

    @property
    def sent(self) -> list[Sent]:
        return [item for c in self.connections for item in c.sent]

    def kinds(self, connection: FakeConnection | None = None) -> list[str]:
        chosen = [connection] if connection is not None else list(self.connections)
        return [kind(item.statement, c.model) for c in chosen for item in c.sent]

    def reset(self) -> None:
        self.connections.clear()
        self.configs.clear()
        self.timeouts.clear()


def install(
    monkeypatch, driver: FakeSql, *, request_timeout_seconds: float | None = None
):
    """Route every ``SqlTransport`` the worker builds to ``driver``.

    # ASSUMPTION: ``opspilot.tools.sql_query.SqlTransport`` is the class the
    # worker instantiates (directly, or through a module that imported the
    # name); patching each module attribute bound to it reaches both.
    """
    from opspilot.tools import sql_query

    original = sql_query.SqlTransport

    class Routed(original):  # type: ignore[misc, valid-type]
        def __init__(self, config, *args, **kwargs):
            kwargs["connector"] = driver.connect
            if request_timeout_seconds is not None:
                kwargs["request_timeout_seconds"] = request_timeout_seconds
            super().__init__(config, *args, **kwargs)

    for name, module in list(sys.modules.items()):
        if not name.startswith("opspilot") or module is None:
            continue
        if getattr(module, "SqlTransport", None) is original:
            monkeypatch.setattr(module, "SqlTransport", Routed)
    return Routed


# -- the worker ------------------------------------------------------------------


def base_env() -> dict[str, str]:
    return {
        PROFILE_ENV: EXT_PROFILE,
        "OPSPILOT_K8S_API_URL": K8S_API,
        "OPSPILOT_K8S_TOKEN": K8S_TOKEN,
        "OPSPILOT_K8S_CA_FILE": os.environ["C6A_TEST_K8S_CA_FILE"],
        "OPSPILOT_K8S_NAMESPACE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        "OPSPILOT_OTEL_PROMETHEUS_USERNAME": PROM_USERNAME,
        "OPSPILOT_OTEL_PROMETHEUS_PASSWORD": PROM_PASSWORD,
    }


def sql_env(url: str | None = PG_URL, **overrides: str) -> dict[str, str]:
    """A worker environment; with a URL, the sql-ro credential too.
    An override of ``""`` removes that key."""
    values = base_env()
    if url is not None:
        values["OPSPILOT_SQL_URL"] = url
        values["OPSPILOT_SQL_USERNAME"] = SQL_USERNAME
        values["OPSPILOT_SQL_PASSWORD"] = SQL_PASSWORD
    values.update(overrides)
    return {k: v for k, v in values.items() if v != ""}


def require_jq() -> None:
    if shutil.which("jq") is None or shutil.which("python3") is None:
        pytest.skip("the extension worker needs jq and python3 on PATH")


def worker(env: Mapping[str, str], *, deadline_seconds: float = 600):
    """The executor a worker of ``env`` builds, for one fresh Run."""
    require_jq()
    clock = FakeClock(start=NOW)
    profile = select_profile(env)
    assert isinstance(profile, ToolProfile)
    if env.get("OPSPILOT_SQL_URL"):
        # Positive control: refusal tests are vacuous without the tools.
        names = {s["function"]["name"] for s in profile.face(clock).tool_schemas}
        assert set(SQL_TOOLS) <= names, names
    lease = _lease()
    deadline = NOW + timedelta(seconds=deadline_seconds)
    store = FakeStore(lease, deadline=deadline)
    sink = RecordingSink()
    factory = profile.executor_factory(store, sink, clock)
    model_input = profile.face(clock).input_for(
        run_id=str(lease.run_id),
        question="Why is checkout degraded?",
        target_id=TARGET_ID,
        deadline=deadline,
        model_requests=12,
    )
    return factory(lease, model_input), sink


def call(tool: str, params: dict[str, object]) -> ToolRequest:
    return ToolRequest(
        step_id="c6a-step",
        tool_index=next(_INDEX),
        tool_name=tool,
        target_ref=TARGET_ID,
        params=params,
        window=FRAME.as_json(),
    )


@dataclass
class Tools:
    """One worker wired to one fake driver."""

    driver: FakeSql
    env: dict[str, str]
    executors: list[Any] = field(default_factory=list)

    def run(self, tool: str, params: dict[str, object]):
        if tool == QUERY and isinstance(params.get("sql"), str):
            self.driver.model = params["sql"]  # type: ignore[assignment]
        executor, _ = worker(self.env)
        self.executors.append(executor)
        return executor.execute(call(tool, params))

    def query(self, sql: str, **params: object):
        return self.run(QUERY, {"sql": sql, **params})


def make_tools(
    monkeypatch,
    dialect: str,
    *,
    url: str | None = None,
    version=None,
    request_timeout_seconds: float | None = None,
    **env: str,
) -> Tools:
    driver = FakeSql(dialect, version)
    install(monkeypatch, driver, request_timeout_seconds=request_timeout_seconds)
    chosen = url or (PG_URL if dialect == "postgresql" else MYSQL_URL)
    return Tools(driver, sql_env(chosen, **env))


# -- outcome helpers -------------------------------------------------------------


def view(outcome) -> Mapping[str, Any]:
    assert outcome.model_view is not None
    return outcome.model_view


def notes(outcome) -> list[str]:
    return [str(n) for n in (view(outcome).get("notes") or [])]


def rows(outcome) -> list[Mapping[str, Any]]:
    content = view(outcome)["content"]
    assert isinstance(content, list), view(outcome)
    return content


def refused_before_request(outcome, driver: FakeSql, *codes: str) -> None:
    """INVALID_PARAMS with one of ``codes`` first in notes; nothing sent."""
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), view(
        outcome
    )
    first = (notes(outcome) or [""])[0]
    assert any(first.startswith(code) for code in codes), (codes, view(outcome))
    assert driver.sent == [], [s.statement for s in driver.sent]


def forbidden(outcome, code: str) -> None:
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_FORBIDDEN"), view(
        outcome
    )
    first = (notes(outcome) or [""])[0]
    assert first.startswith(code), view(outcome)


def number_in(statement: str, params: Sequence[Any] = ()) -> float:
    """The value a ``SET ... = <n>`` statement sets (literal or bound)."""
    match = re.search(r"=\s*'?(\d+(?:\.\d+)?)", statement)
    if match:
        return float(match.group(1))
    assert params, statement
    return float(params[0])


def wait_until(predicate: Callable[[], bool], seconds: float = 3.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()
