"""Per-question databases of the C6 questions (#666 PR-F, #570).

Each of the seven questions starts its own database inside its namespace
(upstream ``before_test``, unchanged) and reaches the host through one
``kubectl port-forward``. For every attempt the driver creates the read-only
account ``opspilot_ro`` with the C6-21 grants (the same list as the C6 task
record), checks it with the product's own account check, and hands the
product only that account (``OPSPILOT_SQL_*`` / ``OPSPILOT_MONGODB_*``).

Admin access always comes from the container's own environment variables;
the driver never reads a password from the question's manifests. The random
read-only password is fed to the container on stdin and never appears in a
process argument.

The table below is data from each question's ``port_forwards``, the
connection parameters of its ``before_test`` and its ``toolsets.yaml`` (a
unit test checks it against the pinned checkout); nothing here reads an
answer.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from scripts.holmes_evals.questions import C6_QUESTIONS

Backend = Literal["sql", "mongodb"]
Dialect = Literal["postgresql", "mysql", "mariadb", "clickhouse", "mssql", "mongodb"]

#: The read-only account every attempt creates (C6-21).
ACCOUNT = "opspilot_ro"
#: How long the new account may take to log in (ClickHouse reloads
#: ``users.d`` on its own; the others answer at once).
LOGIN_WAIT_S = 30.0
PROVISION_TIMEOUT_S = 60.0

#: The questions' admin variables: no product process may hold one.
ADMIN_ENV_NAMES = (
    "POSTGRES_PASSWORD",
    "MYSQL_ROOT_PASSWORD",
    "MARIADB_ROOT_PASSWORD",
    "MSSQL_SA_PASSWORD",
    "MONGO_INITDB_ROOT_PASSWORD",
)


@dataclass(frozen=True)
class DbQuestionSpec:
    qid: str
    backend: Backend
    dialect: Dialect
    namespace: str
    #: ``kubectl exec`` target (``deployment/<name>``), = the forwarded service.
    deployment: str
    local_port: int
    remote_port: int
    #: The database the product connects to.
    database: str
    setup_timeout_s: int

    @property
    def host_ports(self) -> tuple[int, ...]:
        return (self.local_port,)


DB_QUESTIONS: dict[str, DbQuestionSpec] = {
    s.qid: s
    for s in (
        DbQuestionSpec("217_database_sql_query", "sql", "postgresql", "app-217", "postgres", 10217, 5432, "holmesdb", 120),
        DbQuestionSpec("218_mysql_query_performance", "sql", "mysql", "app-218", "mysql", 10218, 3306, "testdb", 120),
        DbQuestionSpec("219_mariadb_query_performance", "sql", "mariadb", "app-219", "mariadb", 10219, 3306, "testdb", 120),
        DbQuestionSpec("222_clickhouse_query_performance", "sql", "clickhouse", "app-222", "clickhouse", 10222, 8123, "default", 120),
        DbQuestionSpec("223_sqlserver_query_performance", "sql", "mssql", "app-223", "sqlserver", 10223, 1433, "testdb", 180),
        DbQuestionSpec("229_mongodb_schema_discovery", "mongodb", "mongodb", "app-229", "mongodb", 10227, 27017, "shopdb", 180),
        DbQuestionSpec("230_mongodb_aggregation_analysis", "mongodb", "mongodb", "app-230", "mongodb", 10228, 27017, "opsdb", 180),
    )
}  # fmt: skip
assert set(DB_QUESTIONS) == set(C6_QUESTIONS)

_SCHEMES: Mapping[str, str] = {
    "postgresql": "postgresql",
    "mysql": "mysql",
    "mariadb": "mariadb",
    "clickhouse": "clickhouse+http",
    "mssql": "mssql",
}
#: MySQL 8's ``caching_sha2_password`` needs TLS without ``cryptography``
#: (C6 record); every other question's server runs without TLS.
_TLS: Mapping[str, str] = {"mysql": "require"}


def product_url(spec: DbQuestionSpec) -> str:
    """The address the product gets (no credentials): the port-forward."""
    if spec.backend == "mongodb":
        return f"mongodb://127.0.0.1:{spec.local_port}"
    return f"{_SCHEMES[spec.dialect]}://127.0.0.1:{spec.local_port}/{spec.database}"


def tls_mode(spec: DbQuestionSpec) -> str:
    return _TLS.get(spec.dialect, "disable")


def product_env(spec: DbQuestionSpec, password: str) -> dict[str, str]:
    """The worker's ``OPSPILOT_SQL_*`` or ``OPSPILOT_MONGODB_*`` settings."""
    if spec.backend == "mongodb":
        return {
            "OPSPILOT_MONGODB_URL": product_url(spec),
            "OPSPILOT_MONGODB_DATABASE": spec.database,
            "OPSPILOT_MONGODB_USERNAME": ACCOUNT,
            "OPSPILOT_MONGODB_PASSWORD": password,
            "OPSPILOT_MONGODB_AUTH_SOURCE": "admin",
            "OPSPILOT_MONGODB_TLS": "disable",
        }
    return {
        "OPSPILOT_SQL_URL": product_url(spec),
        "OPSPILOT_SQL_USERNAME": ACCOUNT,
        "OPSPILOT_SQL_PASSWORD": password,
        "OPSPILOT_SQL_TLS": tls_mode(spec),
    }


def url_env_name(spec: DbQuestionSpec) -> str:
    return "OPSPILOT_MONGODB_URL" if spec.backend == "mongodb" else "OPSPILOT_SQL_URL"


# -- C6-21 grants (one fixed script per dialect; the password comes on stdin) --------


def grant_script(spec: DbQuestionSpec) -> str:
    """The shell script run in the question's container. It reads the
    read-only password from its first stdin line (``$PW``); admin access
    comes from the container's own environment."""
    db = spec.database
    a = ACCOUNT
    if spec.dialect == "postgresql":
        return f"""set -e
read -r PW
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d {db} <<SQL
DROP ROLE IF EXISTS {a};
CREATE ROLE {a} LOGIN PASSWORD '$PW';
GRANT CONNECT ON DATABASE {db} TO {a};
GRANT pg_read_all_data, pg_read_all_stats TO {a};
ALTER ROLE {a} SET default_transaction_read_only = on;
SQL
"""
    if spec.dialect in ("mysql", "mariadb"):
        client, admin = (
            ("mysql", "$MYSQL_ROOT_PASSWORD")
            if spec.dialect == "mysql"
            else ("mariadb", "$MARIADB_ROOT_PASSWORD")
        )
        return f"""set -e
read -r PW
MYSQL_PWD="{admin}" {client} -h127.0.0.1 -uroot <<SQL
DROP USER IF EXISTS '{a}'@'%';
CREATE USER '{a}'@'%' IDENTIFIED BY '$PW';
GRANT SELECT, SHOW VIEW ON {db}.* TO '{a}'@'%';
GRANT SELECT ON performance_schema.* TO '{a}'@'%';
GRANT PROCESS ON *.* TO '{a}'@'%';
SQL
"""
    if spec.dialect == "clickhouse":
        # The question's ``default`` account has no SQL access management:
        # the account is a users.d file, which ClickHouse reloads by itself.
        return f"""set -e
read -r PW
HASH=$(printf %s "$PW" | sha256sum | cut -d' ' -f1)
cat > /etc/clickhouse-server/users.d/{a}.xml <<XML
<clickhouse>
  <profiles><{a}><readonly>2</readonly></{a}></profiles>
  <users>
    <{a}>
      <password_sha256_hex>$HASH</password_sha256_hex>
      <networks><ip>::/0</ip></networks>
      <profile>{a}</profile>
      <quota>default</quota>
      <access_management>0</access_management>
      <grants>
        <query>GRANT SELECT ON {db}.*</query>
        <query>GRANT SELECT ON system.*</query>
      </grants>
    </{a}>
  </users>
</clickhouse>
XML
"""
    if spec.dialect == "mssql":
        return f"""set -e
read -r PW
SQLCMDPASSWORD="$MSSQL_SA_PASSWORD" /opt/mssql-tools18/bin/sqlcmd -C -b -S localhost -U sa -i /dev/stdin <<SQL
IF SUSER_ID('{a}') IS NOT NULL DROP LOGIN {a};
CREATE LOGIN {a} WITH PASSWORD = '$PW', CHECK_POLICY = OFF;
GRANT VIEW SERVER PERFORMANCE STATE TO {a};
GO
USE {db};
CREATE USER {a} FOR LOGIN {a};
ALTER ROLE db_datareader ADD MEMBER {a};
GRANT SHOWPLAN TO {a};
GRANT VIEW DATABASE STATE TO {a};
GRANT VIEW DEFINITION TO {a};
GO
SQL
"""
    return f"""set -e
read -r PW
mongosh --quiet -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin <<JS
const admin = db.getSiblingDB("admin");
if (admin.getUser("{a}")) {{ admin.dropUser("{a}"); }}
admin.createUser({{user: "{a}", pwd: "$PW", roles: [{{role: "read", db: "{db}"}}, {{role: "clusterMonitor", db: "admin"}}]}});
JS
"""


def provision_argv(spec: DbQuestionSpec) -> list[str]:
    """``kubectl exec -i`` with the fixed script; no password in it."""
    return [
        "kubectl", "exec", "-i", "-n", spec.namespace, f"deployment/{spec.deployment}",
        "--", "sh", "-c", grant_script(spec),
    ]  # fmt: skip


class DbSetupError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def provision_db_account(
    spec: DbQuestionSpec, env: Mapping[str, str], password: str
) -> None:
    """Create the read-only account in the question's container (engineer
    kubeconfig). Raises ``DbSetupError("DB_ACCOUNT_SETUP_FAILED")``."""
    try:
        result = subprocess.run(
            provision_argv(spec),
            input=password + "\n",
            capture_output=True,
            text=True,
            env=dict(env),
            timeout=PROVISION_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DbSetupError("DB_ACCOUNT_SETUP_FAILED", type(error).__name__) from None
    if result.returncode != 0:
        raise DbSetupError("DB_ACCOUNT_SETUP_FAILED", f"exit={result.returncode}")


# -- pre-check with the product's own account check ---------------------------------


def _sql_rows(spec: DbQuestionSpec, env: Mapping[str, str]) -> str | None:
    from opspilot.tools import sql_query as sq

    config = sq.SqlConfig.from_env(env)
    assert config is not None
    conn = sq.default_connector(config, 10.0)
    try:
        if config.dialect == "postgresql":
            _, rows, _ = conn.query(sq._PG_ACCOUNT_CHECK, max_rows=64)
        elif config.dialect == "mysql":
            _, rows, _ = conn.query(sq._MYSQL_ACCOUNT_CHECK, max_rows=256)
        elif config.dialect == "clickhouse":
            _, rows, _ = conn.query(sq._CLICKHOUSE_GRANTS_CHECK, max_rows=256)
            problem = sq.session_problem(config.dialect, rows)
            if problem is not None:
                return problem
            _, setting, _ = conn.query(sq._CLICKHOUSE_READONLY_CHECK, max_rows=1)
            readonly = str(setting[0][0]) if setting else ""
            if readonly != "2":
                return f"SQL_ACCOUNT_NOT_READ_ONLY: ClickHouse readonly={readonly}"
            return None
        else:
            _, rows, _ = conn.query(sq._MSSQL_ACCOUNT_CHECK, max_rows=64)
        return sq.session_problem(config.dialect, rows)
    finally:
        conn.close()


def _mongo_status(env: Mapping[str, str]) -> str | None:
    from opspilot.tools import mongodb_query as mq

    config = mq.MongoConfig.from_env(env)
    assert config is not None
    conn = mq.default_connector(config, 10.0)
    try:
        status = conn.command("admin", {"connectionStatus": 1, "showPrivileges": True})
        return mq.account_problem(status)
    finally:
        conn.close()


class DbLoginFailed(RuntimeError):
    pass


def db_precheck(spec: DbQuestionSpec, env: Mapping[str, str]) -> str | None:
    """The product's own account check over the new account: its refusal
    note, or None. A connection that does not log in raises
    ``DbLoginFailed`` (the caller retries until ``LOGIN_WAIT_S``)."""
    try:
        if spec.backend == "mongodb":
            return _mongo_status(env)
        return _sql_rows(spec, env)
    except Exception as error:  # noqa: BLE001 - any driver failure is a login failure
        raise DbLoginFailed(type(error).__name__) from None


def precheck_with_wait(
    spec: DbQuestionSpec,
    env: Mapping[str, str],
    check: Callable[[DbQuestionSpec, Mapping[str, str]], str | None],
    *,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """``check`` until it logs in (at most ``LOGIN_WAIT_S``)."""
    deadline = now() + LOGIN_WAIT_S
    while True:
        try:
            return check(spec, env)
        except DbLoginFailed:
            if now() >= deadline:
                raise
            sleep(2.0)


def summary_block(
    spec: DbQuestionSpec, precheck: str, seconds: float
) -> dict[str, Any]:
    return {
        "user": ACCOUNT,
        "grants": f"C6-21:{spec.dialect}",
        "precheck": precheck,
        "provision_seconds": round(seconds, 3),
    }
