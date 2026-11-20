"""Shared helpers for the #666 PR-F contract tests (C6's seven database
questions in the HolmesGPT eval driver and the Holmes comparison runner).

Written from docs/tasks/2026-11-18-holmes-evals-666.md (section "PR-F 接口")
without reading the PR-F implementation.

Answer isolation (R8-6): question directories are synthetic (allowed keys
plus a synthetic ``expected_output`` sentinel). The real pinned checkout is
only read through the project's own whitelisted ``questions.load_question``
(``port_forwards``, ``setup_timeout``, ``include_tool_calls``,
``memories_generated``); no ``expected_output``, no seed or manifest file.

The fake database connections answer the product's own account-check
statements (``opspilot.tools.sql_query`` / ``mongodb_query``) with grant rows
inside or outside the product's allow-list, so the driver's pre-check runs
the product's ``session_problem`` / ``account_problem`` on them.
Credentials are low-entropy synthetic strings built by concatenation.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tests.holmes_evals_support import write_question

Q217 = "217_database_sql_query"
Q218 = "218_mysql_query_performance"
Q219 = "219_mariadb_query_performance"
Q222 = "222_clickhouse_query_performance"
Q223 = "223_sqlserver_query_performance"
Q229 = "229_mongodb_schema_discovery"
Q230 = "230_mongodb_aggregation_analysis"
C6_SQL = (Q217, Q218, Q219, Q222, Q223)
C6_MONGO = (Q229, Q230)
C6_ALL = C6_SQL + C6_MONGO
RO_USER = "opspilot_ro"


@dataclasses.dataclass(frozen=True)
class Row:
    """The interface's per-question table (``DbQuestionSpec`` values)."""

    qid: str
    backend: str
    dialect: str
    namespace: str
    deployment: str
    local_port: int
    remote_port: int
    database: str
    setup_timeout_s: int
    scheme: str
    tls: str


TABLE: dict[str, Row] = {
    r.qid: r
    for r in (
        Row(Q217, "sql", "postgresql", "app-217", "postgres", 10217, 5432,
            "holmesdb", 120, "postgresql", "disable"),
        Row(Q218, "sql", "mysql", "app-218", "mysql", 10218, 3306,
            "testdb", 120, "mysql", "require"),
        Row(Q219, "sql", "mariadb", "app-219", "mariadb", 10219, 3306,
            "testdb", 120, "mariadb", "disable"),
        Row(Q222, "sql", "clickhouse", "app-222", "clickhouse", 10222, 8123,
            "default", 120, "clickhouse+http", "disable"),
        Row(Q223, "sql", "mssql", "app-223", "sqlserver", 10223, 1433,
            "testdb", 180, "mssql", "disable"),
        Row(Q229, "mongodb", "mongodb", "app-229", "mongodb", 10227, 27017,
            "shopdb", 180, "mongodb", "disable"),
        Row(Q230, "mongodb", "mongodb", "app-230", "mongodb", 10228, 27017,
            "opsdb", 180, "mongodb", "disable"),
    )
}  # fmt: skip

# The admin variable names the containers carry (never their values).
ADMIN_VARS = (
    "POSTGRES_PASSWORD",
    "MYSQL_ROOT_PASSWORD",
    "MARIADB_ROOT_PASSWORD",
    "MSSQL_SA_PASSWORD",
    "MONGO_INITDB_ROOT_PASSWORD",
)

FAKE_DB_PW = "dbpw-" + "fake-" + "21"
FAKE_ADMIN_PW = "admin-" + "fake-" + "22"


def port_forward(qid: str) -> dict[str, Any]:
    r = TABLE[qid]
    return {
        "namespace": r.namespace,
        "service": r.deployment,
        "local_port": r.local_port,
        "remote_port": r.remote_port,
    }


def product_env(qid: str, password: str) -> dict[str, str]:
    """What the interface says the worker gets for a C6 question."""
    r = TABLE[qid]
    if r.backend == "mongodb":
        return {
            "OPSPILOT_MONGODB_URL": f"mongodb://127.0.0.1:{r.local_port}",
            "OPSPILOT_MONGODB_DATABASE": r.database,
            "OPSPILOT_MONGODB_USERNAME": RO_USER,
            "OPSPILOT_MONGODB_PASSWORD": password,
            "OPSPILOT_MONGODB_AUTH_SOURCE": "admin",
            "OPSPILOT_MONGODB_TLS": "disable",
        }
    return {
        "OPSPILOT_SQL_URL": f"{r.scheme}://127.0.0.1:{r.local_port}/{r.database}",
        "OPSPILOT_SQL_USERNAME": RO_USER,
        "OPSPILOT_SQL_PASSWORD": password,
        "OPSPILOT_SQL_TLS": r.tls,
    }


def url_key(qid: str) -> str:
    return (
        "OPSPILOT_MONGODB_URL"
        if TABLE[qid].backend == "mongodb"
        else "OPSPILOT_SQL_URL"
    )


def prefix(qid: str) -> str:
    return "OPSPILOT_MONGODB_" if TABLE[qid].backend == "mongodb" else "OPSPILOT_SQL_"


PROMPTS = {
    Q217: "List the tables in the database, then look at the holmes_eval_217 "
    "table and tell me the verification code stored in it.",
    Q218: "1. List the tables in the database and tell me the verification code "
    "from the holmes_eval_218 table\n2. Why is this query slow?",
    Q219: "1. List the tables and tell me the verification code from "
    "holmes_eval_219\n2. Why is this query slow?",
    Q222: "1. List the tables and tell me the verification code from "
    "holmes_eval_222\n2. Why is this query slow?",
    Q223: "1. List the tables and tell me the verification code from "
    "holmes_eval_223\n2. Why is this query slow?",
    Q229: "What collections exist in the database? Then look at the "
    "transactions collection and tell me the verification code.",
    Q230: "Which service has the most error events in the events collection?",
}


def write_c6_question(upstream: Path, qid: str, *, expected: str) -> Path:
    r = TABLE[qid]
    doc: dict[str, Any] = {
        "user_prompt": PROMPTS[qid],
        "tags": ["db-connectors"],
        "before_test": f"kubectl create namespace {r.namespace}\necho seed\n",
        "after_test": f"kubectl delete namespace {r.namespace} --ignore-not-found\n",
        "setup_timeout": r.setup_timeout_s,
        "port_forwards": [port_forward(qid)],
        "include_tool_calls": True,
        "expected_output": [expected],
    }
    return write_question(upstream, qid, doc)


# -- fake connections for the default pre-check ----------------------------------------

MYSQL_OK = (
    "GRANT PROCESS ON *.* TO `opspilot_ro`@`%`",
    "GRANT SELECT, SHOW VIEW ON `testdb`.* TO `opspilot_ro`@`%`",
    "GRANT SELECT ON `performance_schema`.* TO `opspilot_ro`@`%`",
)
CLICKHOUSE_OK = (
    "GRANT SELECT ON default.* TO opspilot_ro",
    "GRANT SELECT ON system.* TO opspilot_ro",
)
# (rows inside the product's allow-list, rows outside it), per dialect.
GRANT_ROWS: dict[str, tuple[list[tuple[str, ...]], list[tuple[str, ...]]]] = {
    "postgresql": ([], [("relation_write",)]),
    "mssql": ([], [("database_role:db_datawriter",)]),
    "mysql": (
        [(x,) for x in MYSQL_OK],
        [(x,) for x in MYSQL_OK] + [("GRANT INSERT ON `testdb`.* TO `opspilot_ro`@`%`",)],
    ),
    "mariadb": (
        [(x,) for x in MYSQL_OK],
        [(x,) for x in MYSQL_OK] + [("GRANT DELETE ON `testdb`.* TO `opspilot_ro`@`%`",)],
    ),
    "clickhouse": (
        [(x,) for x in CLICKHOUSE_OK],
        [(x,) for x in CLICKHOUSE_OK] + [("GRANT INSERT ON default.* TO opspilot_ro",)],
    ),
}  # fmt: skip


def mongo_status(database: str, *, read_only: bool) -> dict[str, Any]:
    roles = [
        {"role": "read", "db": database},
        {"role": "clusterMonitor", "db": "admin"},
    ]
    if not read_only:
        roles.append({"role": "readWrite", "db": database})
    return {
        "authInfo": {
            "authenticatedUsers": [{"user": RO_USER, "db": "admin"}],
            "authenticatedUserRoles": roles,
            "authenticatedUserPrivileges": [
                {"resource": {"db": database, "collection": ""},
                 "actions": ["find", "listCollections"]}
            ],
        },
        "ok": 1,
    }  # fmt: skip


class FakeSqlConnection:
    """Answers the product's account-check statements with ``grants``."""

    def __init__(self, dialect: str, grants: Sequence[tuple[str, ...]]) -> None:
        self.dialect = dialect
        self.grants = list(grants)
        self.statements: list[str] = []
        self.server_version = "11.4.2-MariaDB" if dialect == "mariadb" else "8.0.36"
        self.closed = False

    def execute(self, statement: str, params: Sequence[object] = ()) -> None:
        self.statements.append(statement)

    def query(
        self, statement: str, params: Sequence[object] = (), *, max_rows: int
    ) -> tuple[list[str], list[tuple[Any, ...]], bool]:
        from opspilot.tools.sql_query import ACCOUNT_CHECK_MARKER

        self.statements.append(statement)
        if ACCOUNT_CHECK_MARKER in statement:
            if "getSetting('readonly')" in statement:
                return ["readonly", "version"], [("2", "24.1.8")], False
            return ["grant"], list(self.grants), False
        if "VERSION()" in statement.upper():
            return ["v", "id"], [(self.server_version, 7)], False
        return [], [], False

    def cancel(self) -> None:
        return

    def close(self) -> None:
        self.closed = True


class FakeMongoConnection:
    def __init__(self, status: Mapping[str, Any]) -> None:
        self.status = dict(status)
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def command(self, database: str, spec: Mapping[str, Any]) -> Mapping[str, Any]:
        self.commands.append((database, dict(spec)))
        if "connectionStatus" in spec:
            return self.status
        if "ping" in spec:
            return {"ok": 1}
        return {"ok": 1}

    def cancel(self) -> None:
        return

    def close(self) -> None:
        self.closed = True


def patch_connectors(monkeypatch: Any, sql: Any = None, mongo: Any = None) -> None:
    """Replace the product's ``default_connector`` (and any alias of it the
    driver's modules hold) with the given factories.

    ASSUMPTION: the default ``db_precheck`` connects through the product's
    ``default_connector`` (looked up at call time, possibly through an alias
    in ``scripts.holmes_evals``); the interface names no seam."""
    import importlib
    import sys

    from opspilot.tools import mongodb_query, sql_query

    pairs = []
    if sql is not None:
        pairs.append((sql_query, sql_query.default_connector, sql))
    if mongo is not None:
        pairs.append((mongodb_query, mongodb_query.default_connector, mongo))
    for module, original, replacement in pairs:
        monkeypatch.setattr(module, "default_connector", replacement)
        for name in ("db_backends", "driver"):
            try:
                importlib.import_module(f"scripts.holmes_evals.{name}")
            except ModuleNotFoundError:
                continue
        for mod_name, module_obj in list(sys.modules.items()):
            if mod_name.startswith("scripts.holmes_evals.") and module_obj is not None:
                for attr, value in list(vars(module_obj).items()):
                    if value is original:
                        monkeypatch.setattr(module_obj, attr, replacement)
