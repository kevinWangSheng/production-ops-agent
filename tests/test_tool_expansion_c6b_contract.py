"""Independent contract tests for tool expansion C6b (#570): the ClickHouse
and SQL Server dialects of ``database_sql_query``,
``database_sql_list_tables`` and ``database_sql_describe_table``.

Written from the C6 task record (docs/tasks/2026-11-20-tool-expansion-c6.md,
「C6b 公开接口」, with the C6a interface it extends), r11 (C6-U1=a),
PRODUCT-CONSTRAINTS line 18 and the C6 pre-review, without reading the
implementation. Red until C6b lands; collection stays green.

Neither dialect has a full read-only session (ClickHouse: GET forces
``readonly=2``; SQL Server: a rollback backstop only), so read-only rests on
the account, the tool's own account check (C6-U1) and the statement check.
These tests pin what the tool sends and when it refuses:

* ClickHouse through a local ``http.server`` stand-in speaking the fixed
  HTTP shape (GET only, fixed query keys, credentials in
  ``X-ClickHouse-User``/``X-ClickHouse-Key``, never ``readonly``, no proxy,
  no redirects), and through the connector seam for the request order;
* SQL Server through the connector seam (``SET`` x3 -> check ->
  ``BEGIN TRANSACTION`` -> model -> ``ROLLBACK TRANSACTION``);
* PostgreSQL and MySQL/MariaDB surfaces byte for byte
  (``tests/fixtures/c6b_sql/pg_mysql_pins.json``, computed on origin/main
  ``0c0492f1`` + interface ``c0c1068c``).

The statement check is in ``test_tool_expansion_c6b_statement_contract.py``;
real servers in ``tests/integration/test_tool_expansion_c6b_sql.py``.
Assumptions beyond the record are marked ``# ASSUMPTION:``. All synthetic
credentials are low-entropy placeholders.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import socket
import ssl
import subprocess
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from opspilot.tools.profiles import PROFILE_ENV, ToolProfile, select_profile
from opspilot.tools.registry import canonical
from opspilot.tracing import check_lab_target
from tests.c6a_sql_support import (
    CANARY,
    DESCRIBE,
    EXT_PROFILE,
    K8S_API,
    LIST_TABLES,
    MYSQL_URL,
    PG_URL,
    QUERY,
    SQL_PASSWORD,
    SQL_TOOLS,
    SQL_USERNAME,
    call,
    forbidden,
    install,
    notes,
    refused_before_request,
    require_jq,
    rows,
    sql_env,
    view,
    worker,
)
from tests.c6b_sql_support import (
    CH_FIXED_KEYS,
    CH_URL,
    MSSQL_URL,
    FakeClickHouse,
    FakeSqlB,
    Reply,
    ch_error,
    env_for,
    is_check,
    kind,
    make_tools_b,
    pytds_error,
    set_request_timeout,
    uuid4_query_id,
    wait_until,
)
from tests.m1_tool_support import FakeClock

PIN_NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
PINS_FILE = Path(__file__).parent / "fixtures" / "c6b_sql" / "pg_mysql_pins.json"
PINS: dict[str, Any] = json.loads(PINS_FILE.read_text())["pins"]
CH, MS = "clickhouse", "mssql"
NEW = (CH, MS)

BACKEND_URLS = {
    "OPSPILOT_ES_URL": "http://127.0.0.1:9200",
    "OPSPILOT_LOKI_URL": "http://127.0.0.1:3100",
    "OPSPILOT_VICTORIALOGS_URL": "http://127.0.0.1:9428",
}
SWITCHES = (
    "OPSPILOT_K8S_LIVE_METRICS",
    "OPSPILOT_KNOWLEDGE_TOOL",
    "OPSPILOT_TODO_WRITE",
)
FLAG_SETS = list(itertools.product((False, True), repeat=6))
DIALECT_URLS = {
    "postgresql": PG_URL,
    "mysql": MYSQL_URL,
    CH: CH_URL,
    MS: MSSQL_URL,
}
PROXY_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)


@pytest.fixture(autouse=True)
def _clean_environment(tmp_path, monkeypatch):
    import os

    for name in list(os.environ):
        if name.startswith("OPSPILOT_SQL") or re.fullmatch(
            r"OPSPILOT_[A-Z0-9_]*DSN", name
        ):
            monkeypatch.delenv(name, raising=False)
    for name in ("PGHOST", "PGPORT", "PGHOSTADDR", "PGSERVICE", "PGDATABASE"):
        monkeypatch.delenv(name, raising=False)
    for name in PROXY_NAMES:
        monkeypatch.delenv(name, raising=False)
    ca_file = tmp_path / "synthetic-k8s-ca.pem"
    ca_file.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("C6A_TEST_K8S_CA_FILE", str(ca_file))


def _flags_key(flags: tuple[bool, ...]) -> str:
    return "".join("1" if f else "0" for f in flags)


def _flags_env(flags: tuple[bool, ...], **extra: str) -> dict[str, str]:
    env = {PROFILE_ENV: EXT_PROFILE}
    for on, name in zip(flags[:3], BACKEND_URLS):
        if on:
            env[name] = BACKEND_URLS[name]
    for on, name in zip(flags[3:], SWITCHES):
        if on:
            env[name] = "1"
    env.update(extra)
    return env


def _face(env: Mapping[str, str]):
    return select_profile(env).face(FakeClock(start=PIN_NOW))


def _tool_names(env: Mapping[str, str]) -> list[str]:
    return [s["function"]["name"] for s in _face(env).tool_schemas]


def _surface_sha256(profile: ToolProfile) -> str:
    face = profile.face(FakeClock(start=PIN_NOW))
    blob = {
        "tool_schemas": [dict(item) for item in face.tool_schemas],
        "variant_id": face.variant_id,
        "evidence_context": dict(face.evidence_context("c6b-run")),
        "versions": profile.versions(),
    }
    return hashlib.sha256(canonical(blob).encode()).hexdigest()


def _revision(env: Mapping[str, str]) -> str:
    return select_profile(env).versions()["tool_schema_revision"]


def _schemas(env: Mapping[str, str]) -> dict[str, Mapping[str, Any]]:
    return {s["function"]["name"]: s for s in _face(env).tool_schemas}


def _web(url: str) -> dict[str, str]:
    return {PROFILE_ENV: EXT_PROFILE, "OPSPILOT_SQL_URL": url}


def _run_real(env: Mapping[str, str], tool: str, params: dict[str, object]):
    """One call through a worker whose transport is the real one."""
    executor, _ = worker(env)
    return executor.execute(call(tool, params))


def _query_real(env, sql: str, **params: object):
    return _run_real(env, QUERY, {"sql": sql, **params})


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(name="clickhouse")
def _clickhouse_stand_in():
    stand_in = FakeClickHouse()
    try:
        yield stand_in
    finally:
        stand_in.close()


# == surfaces and revisions (C6-13) ==============================================


@pytest.mark.parametrize("dialect", ["postgresql", "mysql"])
@pytest.mark.parametrize("flags", FLAG_SETS, ids=_flags_key)
def test_postgres_and_mysql_surfaces_are_byte_identical(dialect, flags):
    revision, surface = PINS[f"{dialect}|{_flags_key(flags)}"]
    env = _flags_env(flags, OPSPILOT_SQL_URL=DIALECT_URLS[dialect])
    assert _revision(env) == revision
    assert _surface_sha256(select_profile(env)) == surface


@pytest.mark.parametrize("dialect", ["postgresql", "mysql"])
@pytest.mark.parametrize("tool", SQL_TOOLS)
def test_postgres_and_mysql_tool_schemas_are_byte_identical(dialect, tool):
    schema = _schemas(_web(DIALECT_URLS[dialect]))[tool]
    digest = hashlib.sha256(canonical(dict(schema)).encode()).hexdigest()
    assert digest == PINS[f"{dialect}|desc|{tool}"]


def test_existing_revision_constants_keep_their_values():
    from opspilot.tools import otel_demo_ext

    assert otel_demo_ext.SQL_POSTGRESQL_TOOL_SCHEMA_REVISION == (
        "otel-demo-ext-e234c8313538"
    )
    assert otel_demo_ext.SQL_MYSQL_TOOL_SCHEMA_REVISION == "otel-demo-ext-764db0f5049f"
    assert otel_demo_ext.tool_schema_revision(sql="postgresql") == (
        "otel-demo-ext-e234c8313538"
    )
    assert otel_demo_ext.tool_schema_revision(sql="mysql") == (
        "otel-demo-ext-764db0f5049f"
    )
    assert otel_demo_ext.tool_schema_revision() == "otel-demo-ext-bdb3eda167eb"
    assert otel_demo_ext.tool_schema_revision(sql=None) == "otel-demo-ext-bdb3eda167eb"


@pytest.mark.parametrize(
    ("url", "dialect"),
    [
        (CH_URL, CH),
        ("clickhouse+https://127.0.0.1:18443/c6tdb", CH),
        (MSSQL_URL, MS),
        ("mssql+pytds://127.0.0.1:11433/c6tdb", MS),
    ],
)
def test_new_dialects_append_three_tools_and_have_their_own_revision(url, dialect):
    from opspilot.tools import otel_demo_ext

    without = {PROFILE_ENV: EXT_PROFILE}
    old_names, new_names = _tool_names(without), _tool_names(_web(url))
    assert new_names[: len(old_names)] == old_names
    assert set(new_names[len(old_names) :]) == set(SQL_TOOLS)
    assert len(new_names) == len(old_names) + 3
    available = _face(_web(url)).evidence_context("c6b-run")["data_sources"][
        "available"
    ]
    assert available["sql"] == sorted(SQL_TOOLS)
    revision = _revision(_web(url))
    assert revision.startswith("otel-demo-ext-")
    assert revision == otel_demo_ext.tool_schema_revision(sql=dialect)
    constant = {
        CH: otel_demo_ext.SQL_CLICKHOUSE_TOOL_SCHEMA_REVISION,
        MS: otel_demo_ext.SQL_MSSQL_TOOL_SCHEMA_REVISION,
    }[dialect]
    assert revision == constant
    known = {pin[0] for key, pin in PINS.items() if "|desc|" not in key}
    known.add("otel-demo-ext-bdb3eda167eb")
    assert revision not in known
    # The web (address only) and the worker (address and credential) agree.
    worker_env = env_for(dialect, url)
    assert select_profile(worker_env).versions() == select_profile(_web(url)).versions()
    assert _surface_sha256(select_profile(worker_env)) == _surface_sha256(
        select_profile(_web(url))
    )
    # C6-15: only tool descriptions change, never the investigation prompt.
    old, new = select_profile(without).versions(), select_profile(_web(url)).versions()
    assert new["prompt_revision"] == old["prompt_revision"]
    assert new["context_policy_revision"] == old["context_policy_revision"]


def test_four_dialects_have_four_distinct_revisions_on_every_backend_set():
    from opspilot.tools import otel_demo_ext

    revisions = set()
    for flags in FLAG_SETS:
        es, loki, vlogs, live, knowledge, todo = flags
        for dialect, url in DIALECT_URLS.items():
            env = _flags_env(flags, OPSPILOT_SQL_URL=url)
            revision = _revision(env)
            assert revision == otel_demo_ext.tool_schema_revision(
                elasticsearch=es,
                loki=loki,
                victorialogs=vlogs,
                live_metrics=live,
                knowledge=knowledge,
                todo=todo,
                sql=dialect,
            )
            revisions.add(revision)
            names = _tool_names(env)
            assert set(names[-3:]) == set(SQL_TOOLS), names[-5:]
    assert len(revisions) == 64 * 4


def test_clickhouse_http_and_https_share_one_dialect_and_revision():
    assert _revision(_web(CH_URL)) == _revision(
        _web("clickhouse+https://127.0.0.1:18443/c6tdb")
    )
    assert _revision(_web(MSSQL_URL)) == _revision(
        _web("mssql+pytds://127.0.0.1:11433/c6tdb")
    )
    assert _revision(_web(CH_URL)) != _revision(_web(MSSQL_URL))


def test_new_dialect_schemas_take_the_c6a_parameters():
    for url in (CH_URL, MSSQL_URL):
        new = _schemas(_web(url))
        pg = _schemas(_web(PG_URL))
        for tool in SQL_TOOLS:
            assert (
                new[tool]["function"]["parameters"]
                == pg[tool]["function"]["parameters"]
            )


@pytest.mark.parametrize(
    ("url", "must"),
    [
        (CH_URL, ("clickhouse", "select", "limit", "settings")),
        (MSSQL_URL, ("sql server", "select", "top", "dm_exec")),
    ],
)
def test_new_dialect_query_descriptions(url, must):
    text = _schemas(_web(url))[QUERY]["function"]["description"]
    lower = text.lower()
    for word in must:
        assert word in lower, (word, text)
    for marker in ("allow_write", "enable write", "write mode", "read_only: false"):
        assert marker not in lower, marker
    others = {
        "postgresql": _schemas(_web(PG_URL))[QUERY]["function"]["description"],
        "mysql": _schemas(_web(MYSQL_URL))[QUERY]["function"]["description"],
    }
    assert text not in others.values()


def test_new_dialect_registrations_are_read_only_otel_demo_tools():
    from opspilot.tools import otel_demo, otel_demo_ext

    # ASSUMPTION: the private registry seam takes the dialect (as C6a's tests).
    for dialect in NEW:
        regs = {r.name: r for r in otel_demo_ext._registrations(sql=dialect)}
        assert regs[QUERY].verb == "query"
        assert regs[LIST_TABLES].verb == "get"
        assert regs[DESCRIBE].verb == "get"
        for name in SQL_TOOLS:
            assert regs[name].read_only is True
            assert regs[name].result_path == ("results",)
            assert regs[name].may_contain_secrets is True
            assert regs[name].source == otel_demo.SOURCE


# == configuration, TLS, startup codes (C6-7, C6-9) ================================

CH_HTTPS = "clickhouse+https://127.0.0.1:18443/c6tdb"
CA_SENTINEL = "<minted-ca>"


def _mint_cert(tmp_path: Path) -> tuple[str, str]:
    import shutil

    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is needed to mint the synthetic certificate")
    cert, key = tmp_path / "ch-cert.pem", tmp_path / "ch-key.pem"
    subprocess.run(
        [
            openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "2",
            "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return str(cert), str(key)


BAD_CONFIG = [
    # TLS combinations (C6b: http only disable; https verify-full or require;
    # SQL Server only disable).
    (CH_URL, {"OPSPILOT_SQL_TLS": "verify-full"}),
    (CH_URL, {"OPSPILOT_SQL_TLS": "require"}),
    # The default is verify-full (C6a), which clickhouse+http does not allow.
    # ASSUMPTION (literal reading; see the ambiguity list): no per-dialect
    # default.
    (CH_URL, {"OPSPILOT_SQL_TLS": ""}),
    (CH_HTTPS, {"OPSPILOT_SQL_TLS": "disable"}),
    (CH_HTTPS, {"OPSPILOT_SQL_TLS": "prefer"}),
    (MSSQL_URL, {"OPSPILOT_SQL_TLS": "verify-full"}),
    (MSSQL_URL, {"OPSPILOT_SQL_TLS": "require"}),
    (MSSQL_URL, {"OPSPILOT_SQL_TLS": ""}),
    ("mssql+pytds://127.0.0.1:1433/db", {"OPSPILOT_SQL_TLS": "require"}),
    (CH_URL, {"OPSPILOT_SQL_CA_FILE": "/run/secrets/ca.pem"}),
    (MSSQL_URL, {"OPSPILOT_SQL_CA_FILE": "/run/secrets/ca.pem"}),
    # Address rules as C6a.
    ("clickhouse+http://u:p@127.0.0.1:8123/db", {}),
    ("clickhouse+http://u@127.0.0.1:8123/db", {}),
    ("clickhouse+http://127.0.0.1:8123/db?readonly=0", {}),
    ("clickhouse+http://127.0.0.1:8123/db?user=default", {}),
    ("clickhouse+http://127.0.0.1:8123/db#x", {}),
    ("clickhouse+http://127.0.0.1:8123", {}),
    ("clickhouse+http://127.0.0.1:8123/", {}),
    ("clickhouse+http://127.0.0.1:8123/a/b", {}),
    ("clickhouse+http://127.0.0.1:8123,127.0.0.2:8123/db", {}),
    ("mssql://sa:x@127.0.0.1:1433/db", {}),
    ("mssql://127.0.0.1:1433/db?encrypt=false", {}),
    ("mssql://127.0.0.1:1433", {}),
    ("mssql://127.0.0.1:1433/a/b", {}),
    ("mssql+pytds://127.0.0.1:1433/db?TrustServerCertificate=yes", {}),
    # Prefixes outside the record.
    ("clickhouse://127.0.0.1:8123/db", {}),
    ("clickhouse+native://127.0.0.1:9000/db", {}),
    ("clickhousedb://127.0.0.1:8123/db", {}),
    ("mssql+pyodbc://127.0.0.1:1433/db", {}),
    ("sqlserver://127.0.0.1:1433/db", {}),
    ("tds://127.0.0.1:1433/db", {}),
]


@pytest.mark.parametrize(("url", "overrides"), BAD_CONFIG)
def test_worker_refuses_bad_c6b_configuration(url, overrides):
    require_jq()
    values = {"OPSPILOT_SQL_TLS": "disable"}
    values.update(overrides)
    with pytest.raises(SystemExit, match="SQL_CONFIG_INVALID"):
        worker(sql_env(url, **values))


@pytest.mark.parametrize(
    ("url", "overrides"),
    [
        (CH_URL, {"OPSPILOT_SQL_TLS": "disable"}),
        ("clickhouse+http://127.0.0.1/c6tdb", {"OPSPILOT_SQL_TLS": "disable"}),
        ("clickhouse+http://ch.c6t.svc:8123/default", {"OPSPILOT_SQL_TLS": "disable"}),
        (CH_HTTPS, {"OPSPILOT_SQL_TLS": ""}),
        (CH_HTTPS, {"OPSPILOT_SQL_TLS": "verify-full"}),
        (CH_HTTPS, {"OPSPILOT_SQL_TLS": "require"}),
        (
            CH_HTTPS,
            {"OPSPILOT_SQL_TLS": "verify-full", "OPSPILOT_SQL_CA_FILE": CA_SENTINEL},
        ),
        (MSSQL_URL, {"OPSPILOT_SQL_TLS": "disable"}),
        ("mssql://127.0.0.1/testdb", {"OPSPILOT_SQL_TLS": "disable"}),
        ("mssql+pytds://127.0.0.1:1433/testdb", {"OPSPILOT_SQL_TLS": "disable"}),
    ],
)
def test_worker_starts_with_valid_c6b_configuration(
    monkeypatch, tmp_path, url, overrides
):
    if overrides.get("OPSPILOT_SQL_CA_FILE") == CA_SENTINEL:
        overrides = {**overrides, "OPSPILOT_SQL_CA_FILE": _mint_cert(tmp_path)[0]}
    driver = FakeSqlB(CH if url.startswith("clickhouse") else MS)
    install(monkeypatch, driver)
    executor, _ = worker(sql_env(url, **overrides))
    assert executor is not None
    assert driver.connections == []  # no connection at startup


@pytest.mark.parametrize(
    "url",
    [
        CH_URL,
        "clickhouse+https://127.0.0.1/x",
        MSSQL_URL,
        "mssql+pytds://127.0.0.1/x",
    ],
)
def test_web_accepts_the_new_prefixes_without_credentials(url):
    profile = select_profile(_web(url))
    profile.versions()
    assert set(SQL_TOOLS) <= set(_tool_names(_web(url)))


@pytest.mark.parametrize(
    "url",
    [
        "clickhouse://127.0.0.1:8123/default",
        "mssql+pyodbc://127.0.0.1:1433/db",
        "sqlserver://127.0.0.1:1433/db",
    ],
)
def test_web_refuses_unknown_prefixes(url):
    with pytest.raises(SystemExit, match="SQL_CONFIG_INVALID"):
        profile = select_profile(_web(url))
        profile.versions()
        profile.face(FakeClock(start=PIN_NOW))


def test_lab_gate_accepts_lab_hosts_of_the_new_dialects():
    env = {
        "OPSPILOT_TRACE": "lab",
        PROFILE_ENV: EXT_PROFILE,
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686",
        "OPSPILOT_K8S_API_URL": K8S_API,
        "LANGSMITH_PROJECT": "opspilot-lab-c6b",
        "LANGSMITH_API_KEY": "lsv2-aaaaaaaa570",
        "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
    }
    assert check_lab_target(env) == ()
    for lab_url in (
        "clickhouse+http://127.0.0.1:10222/default",
        "clickhouse+https://localhost:8443/default",
        "clickhouse+http://clickhouse.c6t.svc:8123/default",
        "mssql://127.0.0.1:10223/testdb",
        "mssql+pytds://localhost/testdb",
        "mssql://sqlserver.c6t.svc:1433/testdb",
    ):
        assert check_lab_target({**env, "OPSPILOT_SQL_URL": lab_url}) == (), lab_url
    for remote in (
        "clickhouse+https://ch.example.invalid:8443/default",
        "clickhouse+http://10.0.0.5:8123/default",
        "mssql://sql.example.invalid:1433/testdb",
        "mssql+pytds://127.attacker.example/testdb",
    ):
        assert "SQL_NOT_LAB" in check_lab_target({**env, "OPSPILOT_SQL_URL": remote}), (
            remote
        )


def _with_dsn(monkeypatch, env: dict[str, str], **dsns: str) -> dict[str, str]:
    for name, value in dsns.items():
        monkeypatch.setenv(name, value)
    return {**env, **dsns}


@pytest.mark.parametrize(
    ("sql_url", "dsns"),
    [
        (
            "clickhouse+http://127.0.0.1/app",  # default port 8123
            {"OPSPILOT_WORKER_DSN": "postgresql://o@localhost:8123/opspilot"},
        ),
        (
            "clickhouse+https://localhost/app",  # default port 8443
            {"OPSPILOT_DSN": "host=127.0.0.1 port=8443 dbname=opspilot user=o"},
        ),
        (
            "mssql://127.0.0.1/app",  # default port 1433
            {"OPSPILOT_WEB_DSN": "postgresql://o@[::1]:1433/opspilot"},
        ),
        (
            "mssql+pytds://localhost:5432/app",
            {"OPSPILOT_WORKER_DSN": "postgresql://o@127.0.0.1/opspilot"},
        ),
        (
            "clickhouse+http://127.0.0.1:5432/app",
            {"OPSPILOT_WORKER_DSN": "dbname=opspilot user=o"},
        ),
    ],
)
def test_worker_refuses_a_c6b_url_on_an_opspilot_database(monkeypatch, sql_url, dsns):
    driver = FakeSqlB(CH if sql_url.startswith("clickhouse") else MS)
    install(monkeypatch, driver)
    tls = "verify-full" if sql_url.startswith("clickhouse+https") else "disable"
    env = _with_dsn(monkeypatch, sql_env(sql_url, OPSPILOT_SQL_TLS=tls), **dsns)
    require_jq()
    with pytest.raises(SystemExit, match="SQL_TARGETS_OPSPILOT_DB"):
        worker(env)
    assert driver.connections == []


@pytest.mark.parametrize(
    ("sql_url", "dsns"),
    [
        (
            "clickhouse+http://127.0.0.1/app",
            {"OPSPILOT_WORKER_DSN": "postgresql://o@127.0.0.1:5432/opspilot"},
        ),
        (
            "mssql://127.0.0.1/app",
            {"OPSPILOT_WORKER_DSN": "postgresql://o@127.0.0.1/opspilot"},
        ),
        (
            "clickhouse+https://127.0.0.1/app",
            {"OPSPILOT_WORKER_DSN": "postgresql://o@127.0.0.1:8123/opspilot"},
        ),
    ],
)
def test_worker_starts_when_default_ports_differ(monkeypatch, sql_url, dsns):
    driver = FakeSqlB(CH if sql_url.startswith("clickhouse") else MS)
    install(monkeypatch, driver)
    tls = "verify-full" if sql_url.startswith("clickhouse+https") else "disable"
    executor, _ = worker(
        _with_dsn(monkeypatch, sql_env(sql_url, OPSPILOT_SQL_TLS=tls), **dsns)
    )
    assert executor is not None


# == ClickHouse: the HTTP shape (real connection, local stand-in) ===================


def test_clickhouse_sends_exactly_three_gets_with_the_fixed_keys(clickhouse):
    sql = "SELECT id, name FROM orders WHERE id < 3"
    clickhouse.model = Reply(names=("id", "name"), rows=[(1, "a"), (2, "b")])
    outcome = _query_real(env_for(CH, clickhouse.url()), sql)
    assert outcome.status == "ok", view(outcome)
    assert rows(outcome) == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    assert view(outcome)["dialect"] == "clickhouse"
    requests = clickhouse.requests
    assert [r.method for r in requests] == ["GET", "GET", "GET"], requests
    assert clickhouse.labels() == ["check:grants", "check:readonly", "model"]
    first, second, third = requests
    assert "/* opspilot:account-check */" in first.query
    assert re.search(r"\bSHOW\s+GRANTS\b", first.query, re.I)
    assert "/* opspilot:account-check */" in second.query
    assert re.search(r"getSetting\(\s*'readonly'\s*\)", second.query)
    assert re.search(r"version\(\s*\)", second.query)
    assert third.query == sql  # verbatim, not rewritten
    ids = set()
    for request in requests:
        assert request.body == b""
        assert set(request.params) == CH_FIXED_KEYS, sorted(request.params)
        for key, values in request.params.items():
            assert len(values) == 1, (key, values)
        assert request.one("database") == "c6tdb"
        assert request.one("default_format") == "JSONCompactEachRowWithNames"
        assert request.one("timeout_overflow_mode") == "throw"
        assert request.one("cancel_http_readonly_queries_on_client_close") == "1"
        assert request.one("output_format_json_quote_64bit_integers") == "0"
        seconds = request.one("max_execution_time")
        assert re.fullmatch(r"\d+", seconds), seconds
        assert 24 <= int(seconds) <= 28, seconds
        assert uuid4_query_id(request.one("query_id")), request.one("query_id")
        ids.add(request.one("query_id"))
    assert len(ids) == 3


def test_clickhouse_never_sends_post_or_a_readonly_setting(clickhouse):
    env = env_for(CH, clickhouse.url())
    for tool, params in (
        (QUERY, {"sql": "SELECT 1 AS n"}),
        (LIST_TABLES, {}),
        (LIST_TABLES, {"schema": "c6tdb", "include_views": False}),
        (DESCRIBE, {"table_name": "orders"}),
    ):
        _run_real(env, tool, params)
    assert clickhouse.requests
    for request in clickhouse.requests:
        assert request.method == "GET"
        flat = {k.lower() for k in request.params}
        assert "readonly" not in flat
        assert not any(k.startswith("readonly") for k in flat)
        # Settings never travel as headers either.
        assert not any("readonly" in h for h in request.headers)


def test_clickhouse_credentials_travel_only_in_the_clickhouse_headers(clickhouse):
    outcome = _query_real(env_for(CH, clickhouse.url()), "SELECT 1 AS n")
    assert outcome.status == "ok", view(outcome)
    for request in clickhouse.requests:
        assert request.headers.get("x-clickhouse-user") == SQL_USERNAME
        assert request.headers.get("x-clickhouse-key") == SQL_PASSWORD
        url_text = request.path + json.dumps(request.params)
        assert SQL_PASSWORD not in url_text
        assert SQL_USERNAME not in url_text
        assert {"user", "password"}.isdisjoint(request.params)
    assert SQL_PASSWORD not in json.dumps(view(outcome), default=str)


def test_clickhouse_ignores_proxy_environment(monkeypatch, clickhouse):
    proxy = FakeClickHouse()
    try:
        proxy_url = f"http://127.0.0.1:{proxy.port}"
        for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
                     "ALL_PROXY", "all_proxy"):  # fmt: skip
            monkeypatch.setenv(name, proxy_url)
        outcome = _query_real(env_for(CH, clickhouse.url()), "SELECT 1 AS n")
        assert outcome.status == "ok", view(outcome)
        assert proxy.requests == []
        assert len(clickhouse.requests) == 3
    finally:
        proxy.close()


@pytest.mark.parametrize("step", ["check:grants", "check:readonly", "model"])
@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_clickhouse_does_not_follow_redirects(clickhouse, step, status):
    elsewhere = FakeClickHouse()
    try:
        clickhouse.replies[step] = Reply(
            status=status, location=f"http://127.0.0.1:{elsewhere.port}/?query=SELECT+1"
        )
        outcome = _query_real(env_for(CH, clickhouse.url()), "SELECT 1 AS n")
        assert outcome.status == "error", view(outcome)
        assert elsewhere.requests == []
        labels = clickhouse.labels()
        assert labels[-1] == step, labels  # nothing after the redirect
    finally:
        elsewhere.close()


def test_clickhouse_catalog_binds_identifiers_as_query_parameters(clickhouse):
    env = env_for(CH, clickhouse.url())
    _run_real(env, LIST_TABLES, {"schema": "c6tschemaq", "include_views": False})
    _run_real(env, DESCRIBE, {"table_name": "c6tschemaq.c6ttableq"})
    catalog = [r for r in clickhouse.requests if clickhouse.label(r) == "catalog"]
    assert catalog, clickhouse.labels()
    bound: list[str] = []
    for request in catalog:
        assert "/* opspilot:catalog */" in request.query
        assert "c6tschemaq" not in request.query
        assert "c6ttableq" not in request.query
        extra = set(request.params) - CH_FIXED_KEYS
        assert extra and all(k.startswith("param_") for k in extra), extra
        for key in extra:
            name = key[len("param_") :]
            assert re.search(r"\{\s*" + re.escape(name) + r"\s*:\s*\w+", request.query)
            bound.append(request.one(key))
        assert request.method == "GET"
    assert "c6tschemaq" in bound and "c6ttableq" in bound, bound
    assert "c6tschemaq.c6ttableq" not in bound
    # Every catalog call is preceded by both account checks.
    labels = clickhouse.labels()
    for n, label in enumerate(labels):
        if label == "catalog":
            assert "check:grants" in labels[:n] and "check:readonly" in labels[:n]


def test_clickhouse_stops_reading_at_limit_plus_one(clickhouse):
    clickhouse.model = Reply(names=("n",), rows=[(n,) for n in range(5000)])
    outcome = _query_real(
        env_for(CH, clickhouse.url()), "SELECT number AS n FROM numbers(5000)", limit=5
    )
    assert outcome.status == "ok", view(outcome)
    assert [r["n"] for r in rows(outcome)] == [0, 1, 2, 3, 4]
    v = view(outcome)
    assert v["incomplete"] is True
    assert "limit" in str(v["incomplete_reason"]).lower()


def test_clickhouse_exactly_limit_rows_are_complete(clickhouse):
    clickhouse.model = Reply(names=("n",), rows=[(n,) for n in range(5)])
    outcome = _query_real(env_for(CH, clickhouse.url()), "SELECT 1 AS n", limit=5)
    assert outcome.status == "ok", view(outcome)
    assert len(rows(outcome)) == 5 and view(outcome)["incomplete"] is False


def test_clickhouse_raw_rows_over_2_mib_are_refused_whole(clickhouse):
    clickhouse.model = Reply(
        names=("id", "body"), rows=[(n, "q" * 5000) for n in range(600)]
    )
    outcome = _query_real(
        env_for(CH, clickhouse.url()), "SELECT id, body FROM big", limit=500
    )
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE"), view(
        outcome
    )
    assert view(outcome)["content"] is None


def test_clickhouse_zero_rows_are_no_data(clickhouse):
    clickhouse.model = Reply(names=("n",), rows=())
    outcome = _query_real(env_for(CH, clickhouse.url()), "SELECT 1 AS n WHERE 0")
    assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA"), view(outcome)


def test_clickhouse_exception_text_inside_a_200_stream_is_an_error(clickhouse):
    clickhouse.model = Reply(
        names=("n",),
        rows=[(1,), (2,), (3,)],
        midstream_error=(
            "Code: 241. DB::Exception: Memory limit (total) exceeded: would use "
            f"9.31 GiB. password={CANARY} (MEMORY_LIMIT_EXCEEDED)"
        ),
    )
    outcome = _query_real(env_for(CH, clickhouse.url()), "SELECT number AS n FROM t")
    assert outcome.status == "error", view(outcome)
    assert outcome.reason in ("SOURCE_UNAVAILABLE", "SOURCE_ERROR"), view(outcome)
    assert CANARY not in json.dumps(view(outcome), default=str)


def test_clickhouse_values_keep_64bit_integers_as_numbers(clickhouse):
    big = 2**63 + 5
    clickhouse.model = Reply(
        names=("u64", "s", "f", "nul", "arr"),
        rows=[(big, "x", 1.5, None, [1, 2])],
    )
    outcome = _query_real(env_for(CH, clickhouse.url()), "SELECT 1")
    assert outcome.status == "ok", view(outcome)
    [row] = rows(outcome)
    assert row["u64"] == big
    assert row["s"] == "x" and row["f"] == 1.5 and row["nul"] is None
    assert row["arr"] == "[1,2]"  # arrays as compact JSON strings (C6a)


def test_clickhouse_duplicate_column_names_get_suffixes(clickhouse):
    clickhouse.model = Reply(names=("a", "a", "b"), rows=[(1, 2, 3)])
    outcome = _query_real(
        env_for(CH, clickhouse.url()), "SELECT 1 AS a, 2 AS a, 3 AS b"
    )
    assert outcome.status == "ok", view(outcome)
    [row] = rows(outcome)
    assert list(row)[:3] == ["a", "a#2", "b"]


def test_clickhouse_watchdog_closes_the_connection_past_the_deadline(
    monkeypatch, clickhouse
):
    set_request_timeout(monkeypatch, 2.0)
    clickhouse.model = Reply(block_seconds=15)
    started = time.monotonic()
    outcome = _query_real(env_for(CH, clickhouse.url()), "SELECT count() FROM huge")
    elapsed = time.monotonic() - started
    assert (outcome.status, outcome.reason) == ("timeout", "TOOL_TIMEOUT"), view(
        outcome
    )
    assert elapsed < 8, elapsed
    # The server cancels on client close (cancel_http_readonly_queries_on_client_close).
    assert clickhouse.disconnected.wait(5)
    # The server-side limit is the budget minus two seconds, at least one.
    for request in clickhouse.requests:
        assert request.one("max_execution_time") == "1", request.params


CH_ERRORS = [
    *[
        (code, 400, "INVALID_PARAMS")
        for code in (62, 47, 43, 53, 42, 6, 27, 38, 41, 69, 70, 72)
    ],
    *[(code, 404, "INVALID_PARAMS") for code in (60, 46, 81, 16)],
    (497, 403, "SOURCE_FORBIDDEN"),
    (516, 403, "SOURCE_FORBIDDEN"),
    (192, 403, "SOURCE_FORBIDDEN"),
    (194, 401, "SOURCE_FORBIDDEN"),
    (164, 403, "SOURCE_FORBIDDEN"),
    (159, 500, "TOOL_TIMEOUT"),
    (394, 500, "TOOL_TIMEOUT"),
    (241, 500, "SOURCE_UNAVAILABLE"),
    (202, 500, "SOURCE_UNAVAILABLE"),
    (210, 500, "SOURCE_UNAVAILABLE"),
    (1002, 400, "SOURCE_ERROR"),
]


@pytest.mark.parametrize(("code", "status", "reason"), CH_ERRORS)
def test_clickhouse_errors_on_the_model_request(clickhouse, code, status, reason):
    message = f"server said something password={CANARY} " + "w" * 3000
    clickhouse.model = ch_error(code, message, status)
    outcome = _query_real(env_for(CH, clickhouse.url()), "SELECT 1")
    expected_status = "timeout" if reason == "TOOL_TIMEOUT" else "error"
    assert (outcome.status, outcome.reason) == (expected_status, reason), view(outcome)
    text = json.dumps(view(outcome), default=str)
    assert CANARY not in text
    assert "w" * 600 not in text
    if reason == "INVALID_PARAMS":
        assert "server said something" in text  # echoed, cut and marked untrusted
    if reason == "SOURCE_FORBIDDEN":
        if code == 164:
            assert "SESSION_READ_ONLY" in " ".join(notes(outcome)), view(outcome)
        else:
            assert "server said something" not in text  # not echoed
    assert len(clickhouse.requests) == 3  # no retry


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "SOURCE_FORBIDDEN"),
        (403, "SOURCE_FORBIDDEN"),
        (502, "SOURCE_UNAVAILABLE"),
        (503, "SOURCE_UNAVAILABLE"),
    ],
)
def test_clickhouse_http_status_without_an_exception_code(clickhouse, status, reason):
    clickhouse.model = Reply(status=status, raw_lines=["upstream said no"])
    outcome = _query_real(env_for(CH, clickhouse.url()), "SELECT 1")
    assert (outcome.status, outcome.reason) == ("error", reason), view(outcome)


def test_clickhouse_bad_password_on_the_first_request_stops_everything(clickhouse):
    clickhouse.replies["check:grants"] = ch_error(
        516,
        f"{SQL_USERNAME}: Authentication failed: password is incorrect {CANARY}",
        403,
    )
    outcome = _query_real(env_for(CH, clickhouse.url()), "SELECT 1 AS marker_c6t")
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_FORBIDDEN"), view(
        outcome
    )
    assert clickhouse.labels() == ["check:grants"]
    assert CANARY not in json.dumps(view(outcome), default=str)


def test_clickhouse_unreachable_server_is_source_unavailable():
    url = f"clickhouse+http://127.0.0.1:{_free_port()}/c6tdb"
    outcome = _query_real(env_for(CH, url), "SELECT 1")
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_UNAVAILABLE"), view(
        outcome
    )


def test_clickhouse_admin_account_is_refused_and_the_model_request_never_sent(
    clickhouse,
):
    clickhouse.grants = ["GRANT ALL ON *.* TO default WITH GRANT OPTION"]
    for tool, params in (
        (QUERY, {"sql": "SELECT 1 AS marker_c6t"}),
        (LIST_TABLES, {}),
        (DESCRIBE, {"table_name": "orders"}),
    ):
        outcome = _run_real(env_for(CH, clickhouse.url()), tool, params)
        forbidden(outcome, "SQL_ACCOUNT_NOT_READ_ONLY")
    assert "model" not in clickhouse.labels() and "catalog" not in clickhouse.labels()


@pytest.mark.parametrize("tls", ["require", "verify-full-with-ca", "verify-full"])
def test_clickhouse_https_tls_modes(tmp_path, tls):
    cert, key = _mint_cert(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server = FakeClickHouse(tls=context)
    try:
        url = server.url(scheme="clickhouse+https")
        overrides = {
            "require": {"OPSPILOT_SQL_TLS": "require"},
            "verify-full-with-ca": {
                "OPSPILOT_SQL_TLS": "verify-full",
                "OPSPILOT_SQL_CA_FILE": cert,
            },
            "verify-full": {"OPSPILOT_SQL_TLS": "verify-full"},
        }[tls]
        outcome = _query_real(sql_env(url, **overrides), "SELECT 1 AS n")
        if tls == "verify-full":
            # A self-signed certificate is not in the system trust store.
            assert (outcome.status, outcome.reason) == (
                "error",
                "SOURCE_UNAVAILABLE",
            ), view(outcome)
            assert server.requests == []
        else:
            assert outcome.status == "ok", view(outcome)
            assert len(server.requests) == 3
    finally:
        server.close()


# == ClickHouse through the connector seam ==========================================


def test_clickhouse_request_order_through_the_connector(monkeypatch):
    tools = make_tools_b(monkeypatch, CH)
    sql = "SELECT id FROM orders"
    outcome = tools.query(sql)
    assert outcome.status == "ok", view(outcome)
    [connection] = tools.driver.connections
    assert tools.driver.kinds(connection) == ["check:grants", "check:readonly", "model"]
    assert [s.method for s in connection.sent] == ["query", "query", "query"]
    # No session, no transaction, no settings statement.
    for item in connection.sent:
        text = item.statement.upper()
        assert not text.lstrip().startswith(("SET ", "BEGIN", "START", "ROLLBACK"))
    assert connection.closed >= 1


@pytest.mark.parametrize(
    "line",
    [
        "GRANT INSERT ON c6tdb.* TO c6t_ro",
        "GRANT SELECT, INSERT ON c6tdb.* TO c6t_ro",
        "GRANT ALL ON *.* TO default WITH GRANT OPTION",
        "GRANT ALL ON c6tdb.* TO c6t_ro",
        "GRANT SELECT ON c6tdb.* TO c6t_ro WITH GRANT OPTION",
        "GRANT ALTER UPDATE ON c6tdb.* TO c6t_ro",
        "GRANT ALTER DELETE ON c6tdb.* TO c6t_ro",
        "GRANT ALTER TABLE ON c6tdb.* TO c6t_ro",
        "GRANT CREATE TABLE ON c6tdb.* TO c6t_ro",
        "GRANT CREATE TEMPORARY TABLE ON *.* TO c6t_ro",
        "GRANT DROP TABLE ON c6tdb.* TO c6t_ro",
        "GRANT TRUNCATE ON c6tdb.* TO c6t_ro",
        "GRANT OPTIMIZE ON c6tdb.* TO c6t_ro",
        "GRANT SYSTEM ON *.* TO c6t_ro",
        "GRANT SYSTEM FLUSH LOGS ON *.* TO c6t_ro",
        "GRANT KILL QUERY ON *.* TO c6t_ro",
        "GRANT ACCESS MANAGEMENT ON *.* TO c6t_ro",
        "GRANT SHOW USERS ON *.* TO c6t_ro",
        "GRANT INTROSPECTION ON *.* TO c6t_ro",
        "GRANT SOURCES ON *.* TO c6t_ro",
        "GRANT URL ON *.* TO c6t_ro",
        "GRANT FILE ON *.* TO c6t_ro",
        "GRANT S3 ON *.* TO c6t_ro",
        "GRANT REMOTE ON *.* TO c6t_ro",
        "GRANT dictGet ON *.* TO c6t_ro",
        "GRANT c6t_writer TO c6t_ro",
        "GRANT c6t_writer TO c6t_ro WITH ADMIN OPTION",
        "REVOKE SELECT ON system.* FROM c6t_ro",
        "GRANT SELECT ON c6tdb.* TO c6t_ro; GRANT INSERT ON *.* TO c6t_ro",
        "GRANT SELECT ON a.* TO c6t_ro GRANT INSERT ON b.* TO c6t_ro",
        "GRANT SELECT(id), INSERT(id) ON c6tdb.orders TO c6t_ro",
        "this is not a grant",
        "",
    ],
)
@pytest.mark.parametrize("tool", SQL_TOOLS)
def test_clickhouse_grant_problems_refuse_without_the_model_statement(
    monkeypatch, line, tool
):
    tools = make_tools_b(monkeypatch, CH)
    tools.driver.grants = [*tools.driver.grants, line]
    params = {
        QUERY: {"sql": "SELECT 1 AS marker_c6t"},
        LIST_TABLES: {},
        DESCRIBE: {"table_name": "orders"},
    }[tool]
    outcome = tools.run(tool, params)
    forbidden(outcome, "SQL_ACCOUNT_NOT_READ_ONLY")
    kinds = tools.driver.kinds()
    assert "check:grants" in kinds
    assert "model" not in kinds and "catalog" not in kinds, kinds


@pytest.mark.parametrize(
    "lines",
    [
        ["GRANT SELECT ON c6tdb.* TO c6t_ro"],
        ["GRANT SELECT ON c6tdb.* TO c6t_ro", "GRANT SELECT ON system.* TO c6t_ro"],
        ["GRANT SELECT(id, name) ON c6tdb.orders TO c6t_ro"],
        ["GRANT SHOW ON *.* TO c6t_ro"],
        ["GRANT SHOW DATABASES ON *.* TO c6t_ro"],
        ["GRANT SHOW TABLES ON c6tdb.* TO c6t_ro"],
        ["GRANT SHOW COLUMNS ON c6tdb.* TO c6t_ro"],
        ["GRANT SHOW DICTIONARIES ON *.* TO c6t_ro"],
        ["GRANT SELECT, SHOW TABLES ON c6tdb.* TO c6t_ro"],
    ],
)
def test_clickhouse_whitelisted_grants_pass(monkeypatch, lines):
    tools = make_tools_b(monkeypatch, CH)
    tools.driver.grants = lines
    outcome = tools.query("SELECT 1 AS n")
    assert outcome.status == "ok", view(outcome)
    assert "model" in tools.driver.kinds()


@pytest.mark.parametrize("tool", SQL_TOOLS)
def test_clickhouse_readonly_1_is_account_unsupported(monkeypatch, tool):
    tools = make_tools_b(monkeypatch, CH)
    tools.driver.readonly = 1
    params = {
        QUERY: {"sql": "SELECT 1 AS marker_c6t"},
        LIST_TABLES: {},
        DESCRIBE: {"table_name": "orders"},
    }[tool]
    outcome = tools.run(tool, params)
    forbidden(outcome, "SQL_ACCOUNT_UNSUPPORTED")
    assert "readonly" in " ".join(notes(outcome)).lower()
    kinds = tools.driver.kinds()
    assert "check:readonly" in kinds
    assert "model" not in kinds and "catalog" not in kinds, kinds


def test_clickhouse_readonly_2_passes(monkeypatch):
    tools = make_tools_b(monkeypatch, CH)
    tools.driver.readonly = 2
    assert tools.query("SELECT 1 AS n").status == "ok"


def test_clickhouse_readonly_1_over_http_never_sends_the_model(clickhouse):
    clickhouse.readonly = 1
    outcome = _query_real(env_for(CH, clickhouse.url()), "SELECT 1 AS marker_c6t")
    forbidden(outcome, "SQL_ACCOUNT_UNSUPPORTED")
    assert "model" not in clickhouse.labels()


def test_clickhouse_failing_account_check_never_runs_the_model(clickhouse):
    clickhouse.replies["check:readonly"] = ch_error(1002, "boom", 500)
    outcome = _query_real(env_for(CH, clickhouse.url()), "SELECT 1 AS marker_c6t")
    assert outcome.status == "error", view(outcome)
    assert "model" not in clickhouse.labels()


# == SQL Server through the connector seam ==========================================

MS_SEQUENCE = [
    "set:lock_timeout",
    "set:xact_abort",
    "set:implicit_transactions",
    "check",
    "begin",
    "model",
    "rollback",
]


def test_mssql_sequence_sets_check_transaction_model_rollback(monkeypatch):
    tools = make_tools_b(monkeypatch, MS)
    sql = "SELECT TOP 2 id, name FROM dbo.orders"
    tools.driver.result = (["id", "name"], [(1, "a"), (2, "b")])
    outcome = tools.query(sql)
    assert outcome.status == "ok", view(outcome)
    assert rows(outcome) == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    assert view(outcome)["dialect"] == "mssql"
    [connection] = tools.driver.connections
    assert tools.driver.kinds(connection) == MS_SEQUENCE
    by_kind = {kind(s.statement, sql): s for s in connection.sent}
    assert re.fullmatch(
        r"\s*SET\s+XACT_ABORT\s+ON\s*;?\s*", by_kind["set:xact_abort"].statement, re.I
    )
    assert re.fullmatch(
        r"\s*SET\s+IMPLICIT_TRANSACTIONS\s+OFF\s*;?\s*",
        by_kind["set:implicit_transactions"].statement,
        re.I,
    )
    lock = by_kind["set:lock_timeout"]
    match = re.fullmatch(r"\s*SET\s+LOCK_TIMEOUT\s+(\d+)\s*;?\s*", lock.statement, re.I)
    assert match, lock.statement
    assert 1_000 <= int(match.group(1)) <= 28_000
    assert "/* opspilot:account-check */" in by_kind["check"].statement
    assert re.fullmatch(
        r"\s*BEGIN\s+TRAN(SACTION)?\s*;?\s*", by_kind["begin"].statement, re.I
    )
    assert re.fullmatch(
        r"\s*ROLLBACK(\s+TRAN(SACTION)?)?\s*;?\s*", by_kind["rollback"].statement, re.I
    )
    model = by_kind["model"]
    assert model.method == "query" and not model.params
    assert connection.closed >= 1
    [timeout] = tools.driver.timeouts
    assert 25 <= timeout <= 30, timeout


def test_mssql_rolls_back_even_when_the_model_statement_fails(monkeypatch):
    tools = make_tools_b(monkeypatch, MS)
    tools.driver.errors["model"] = RuntimeError("driver blew up")
    outcome = tools.query("SELECT 1")
    assert outcome.status == "error", view(outcome)
    kinds = tools.driver.kinds()
    assert kinds.index("model") < len(kinds) - 1, kinds
    assert kinds[kinds.index("model") + 1 :] == ["rollback"], kinds
    assert all(c.closed >= 1 for c in tools.driver.connections)


def test_mssql_transaction_that_cannot_start_never_runs_the_model(monkeypatch):
    tools = make_tools_b(monkeypatch, MS)
    tools.driver.errors["begin"] = RuntimeError("cannot begin")
    outcome = tools.query("SELECT 1")
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_ERROR"), view(outcome)
    assert "model" not in tools.driver.kinds()
    assert all(c.closed >= 1 for c in tools.driver.connections)


@pytest.mark.parametrize(
    "failing", ["set:lock_timeout", "set:xact_abort", "set:implicit_transactions"]
)
def test_mssql_a_failing_set_never_runs_the_model(monkeypatch, failing):
    tools = make_tools_b(monkeypatch, MS)
    tools.driver.errors[failing] = RuntimeError("cannot set")
    outcome = tools.query("SELECT 1")
    assert outcome.status == "error", view(outcome)
    assert "model" not in tools.driver.kinds()


@pytest.mark.parametrize("dialect", NEW)
def test_each_call_opens_its_own_connection_and_checks_the_account(
    monkeypatch, dialect
):
    tools = make_tools_b(monkeypatch, dialect)
    for n in range(3):
        assert tools.query(f"SELECT {n} AS n").status == "ok"
    assert len(tools.driver.connections) == 3
    for connection in tools.driver.connections:
        assert connection.closed >= 1
        kinds = tools.driver.kinds(connection)
        assert any(is_check(k) for k in kinds)
        assert kinds.count("model") == 1


@pytest.mark.parametrize(
    "problems",
    [
        ["sysadmin"],
        ["db_owner"],
        ["db_datawriter"],
        ["server_permission"],
        ["database_permission"],
        ["object_permission", "column_permission"],
        ["schema_owner"],
    ],
)
@pytest.mark.parametrize("tool", SQL_TOOLS)
def test_mssql_account_problems_refuse_without_the_model_statement(
    monkeypatch, problems, tool
):
    # ASSUMPTION: like PostgreSQL's, the check answers one problem code per
    # row; any row refuses (codes are the implementation's choice).
    tools = make_tools_b(monkeypatch, MS)
    tools.driver.problems = problems
    params = {
        QUERY: {"sql": "SELECT 1 AS marker_c6t"},
        LIST_TABLES: {},
        DESCRIBE: {"table_name": "orders"},
    }[tool]
    outcome = tools.run(tool, params)
    forbidden(outcome, "SQL_ACCOUNT_NOT_READ_ONLY")
    kinds = tools.driver.kinds()
    assert "check" in kinds
    assert "model" not in kinds and "catalog" not in kinds and "begin" not in kinds, (
        kinds
    )
    assert set(kinds[kinds.index("check") + 1 :]) <= {"rollback"}, kinds
    assert all(c.closed >= 1 for c in tools.driver.connections)


def test_mssql_session_problem_seam():
    from opspilot.tools import sql_query

    # ASSUMPTION: rows as the check returns them, one problem code per row.
    assert sql_query.session_problem("mssql", []) is None
    assert sql_query.session_problem("mssql", [("sysadmin",)]) is not None


def test_mssql_catalog_queries_are_marked_bound_and_inside_the_transaction(monkeypatch):
    tools = make_tools_b(monkeypatch, MS)
    for tool, params in (
        (LIST_TABLES, {"schema": "c6tschemaq", "include_views": False}),
        (DESCRIBE, {"table_name": "c6tschemaq.c6ttableq"}),
    ):
        tools.run(tool, params)
    for connection in tools.driver.connections:
        kinds = tools.driver.kinds(connection)
        assert "catalog" in kinds, kinds
        first = kinds.index("catalog")
        last = len(kinds) - 1 - kinds[::-1].index("catalog")
        assert kinds.index("check") < kinds.index("begin") < first
        assert kinds[last + 1 :] == ["rollback"], kinds
        for item in connection.sent:
            if kind(item.statement) == "catalog":
                assert "c6tschemaq" not in item.statement
                assert "c6ttableq" not in item.statement
    bound = json.dumps(
        [
            list(map(str, s.params or ()))
            for s in tools.driver.sent
            if kind(s.statement) == "catalog"
        ]
    )
    assert "c6tschemaq" in bound and "c6ttableq" in bound, bound
    assert "c6tschemaq.c6ttableq" not in bound


@pytest.mark.parametrize("dialect", NEW)
def test_describe_of_a_missing_table_is_table_not_found(monkeypatch, dialect):
    tools = make_tools_b(monkeypatch, dialect)
    outcome = tools.run(DESCRIBE, {"table_name": "nope"})
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), view(
        outcome
    )
    assert notes(outcome)[0].startswith("TABLE_NOT_FOUND"), view(outcome)


@pytest.mark.parametrize("dialect", NEW)
@pytest.mark.parametrize(
    ("tool", "params"),
    [
        (LIST_TABLES, {"schema": "x';DROP TABLE t;--"}),
        (LIST_TABLES, {"schema": "[dbo]"}),
        (LIST_TABLES, {"schema": "`db`"}),
        (DESCRIBE, {"table_name": "t{p0:String}"}),
        (DESCRIBE, {"table_name": "t]; EXEC xp_cmdshell 'x' --"}),
        (DESCRIBE, {"table_name": "t" * 129}),
    ],
)
def test_bad_identifiers_are_refused_before_any_request(
    monkeypatch, dialect, tool, params
):
    tools = make_tools_b(monkeypatch, dialect)
    outcome = tools.run(tool, params)
    refused_before_request(outcome, tools.driver, "INVALID_IDENTIFIER")


def test_mssql_watchdog_cancels_a_query_past_the_deadline(monkeypatch):
    tools = make_tools_b(monkeypatch, MS, request_timeout_seconds=2.0)
    tools.driver.block_seconds = 15
    started = time.monotonic()
    outcome = tools.query("SELECT COUNT_BIG(*) FROM big_a, big_b")
    elapsed = time.monotonic() - started
    assert (outcome.status, outcome.reason) == ("timeout", "TOOL_TIMEOUT"), view(
        outcome
    )
    assert elapsed < 8, elapsed
    [connection] = tools.driver.connections
    assert wait_until(lambda: connection.cancel_calls >= 1)
    assert wait_until(lambda: connection.closed >= 1)


# == SQL Server error classification (python-tds raw exceptions) =====================

MS_ERRORS = [
    *[
        ("ProgrammingError", n, "INVALID_PARAMS")
        for n in (102, 156, 170, 207, 208, 4104)
    ],
    *[("OperationalError", n, "INVALID_PARAMS") for n in (8134, 245, 8114)],
    *[("OperationalError", n, "SOURCE_FORBIDDEN") for n in (229, 230, 262, 297, 300)],
    ("OperationalError", 1222, "TOOL_TIMEOUT"),
    ("OperationalError", 9002, "SOURCE_ERROR"),
]


@pytest.mark.parametrize(("cls_name", "number", "reason"), MS_ERRORS)
def test_mssql_errors_on_the_model_statement(monkeypatch, cls_name, number, reason):
    tools = make_tools_b(monkeypatch, MS)
    tools.driver.errors["model"] = pytds_error(
        cls_name, number, f"server said something password={CANARY} " + "w" * 3000
    )
    outcome = tools.query("SELECT 1")
    expected_status = "timeout" if reason == "TOOL_TIMEOUT" else "error"
    assert (outcome.status, outcome.reason) == (expected_status, reason), view(outcome)
    text = json.dumps(view(outcome), default=str)
    assert CANARY not in text
    assert "w" * 600 not in text
    if reason == "INVALID_PARAMS":
        assert "server said something" in text
    if reason == "SOURCE_FORBIDDEN":
        assert "server said something" not in text
    assert len(tools.driver.connections) == 1
    assert "rollback" in tools.driver.kinds()  # the backstop still runs


def test_mssql_driver_timeout_is_tool_timeout(monkeypatch):
    tools = make_tools_b(monkeypatch, MS)
    tools.driver.errors["model"] = pytds_error("TimeoutError", 0, "Timeout")
    outcome = tools.query("SELECT 1")
    assert (outcome.status, outcome.reason) == ("timeout", "TOOL_TIMEOUT"), view(
        outcome
    )


def test_mssql_login_failed_is_forbidden_and_not_echoed(monkeypatch):
    tools = make_tools_b(monkeypatch, MS)
    tools.driver.connect_error = pytds_error(
        "LoginError", 18456, f"Login failed for user '{SQL_USERNAME}'. {CANARY}"
    )
    outcome = tools.query("SELECT 1")
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_FORBIDDEN"), view(
        outcome
    )
    text = json.dumps(view(outcome), default=str)
    assert CANARY not in text and SQL_PASSWORD not in text
    assert len(tools.driver.configs) == 1  # no retry


def test_mssql_failure_before_login_is_source_unavailable(monkeypatch):
    tools = make_tools_b(monkeypatch, MS)
    error = pytds_error(
        "LoginError", 0, "Cannot connect to server '127.0.0.1': refused"
    )
    tools.driver.connect_error = error
    outcome = tools.query("SELECT 1")
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_UNAVAILABLE"), view(
        outcome
    )
    tools.driver.connect_error = ConnectionRefusedError(61, "Connection refused")
    outcome = tools.query("SELECT 1")
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_UNAVAILABLE"), view(
        outcome
    )


def test_mssql_connection_lost_mid_query_is_source_unavailable(monkeypatch):
    tools = make_tools_b(monkeypatch, MS)
    tools.driver.errors["model"] = pytds_error(
        "ClosedConnectionError", 0, "Server closed connection"
    )
    outcome = tools.query("SELECT 1")
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_UNAVAILABLE"), view(
        outcome
    )


# == projection, limits and time notes on the new dialects (as C6a) =================


@pytest.mark.parametrize("dialect", NEW)
def test_password_and_token_columns_are_redacted(monkeypatch, dialect):
    tools = make_tools_b(monkeypatch, dialect)
    tools.driver.result = (
        ["id", "password", "api_token"],
        [(1, f"pw-{CANARY}", f"tok-{CANARY}")],
    )
    outcome = tools.query("SELECT id, password, api_token FROM users")
    assert outcome.status == "ok", view(outcome)
    text = json.dumps(view(outcome))
    assert CANARY not in text and "REDACTED" in text
    assert rows(outcome)[0]["id"] == 1


@pytest.mark.parametrize("dialect", NEW)
@pytest.mark.parametrize("limit", [1, 500])
def test_reaching_the_limit_marks_incomplete(monkeypatch, dialect, limit):
    tools = make_tools_b(monkeypatch, dialect)
    tools.driver.result = (["n"], [(n,) for n in range(limit + 5)])
    sql = "SELECT n FROM numbers_table"
    outcome = tools.query(sql, limit=limit)
    assert outcome.status == "ok", view(outcome)
    assert [r["n"] for r in rows(outcome)] == list(range(limit))
    assert view(outcome)["incomplete"] is True
    [model] = [s for s in tools.driver.sent if s.statement == sql]
    assert model.max_rows in (limit, limit + 1)


@pytest.mark.parametrize("dialect", NEW)
def test_long_values_are_cut_and_zero_rows_are_no_data(monkeypatch, dialect):
    tools = make_tools_b(monkeypatch, dialect)
    tools.driver.result = (["id", "body"], [(1, "x" * 5000)])
    outcome = tools.query("SELECT id, body FROM docs")
    assert outcome.status == "ok", view(outcome)
    [row] = rows(outcome)
    assert row["_truncated_fields"] == ["body"] and len(row["body"]) <= 2064
    tools.driver.result = (["n"], [])
    empty = tools.query("SELECT n FROM empty_table")
    assert (empty.status, empty.reason) == ("no_data", "NO_DATA"), view(empty)


@pytest.mark.parametrize("dialect", NEW)
def test_limit_outside_1_to_500_is_refused_before_any_request(monkeypatch, dialect):
    tools = make_tools_b(monkeypatch, dialect)
    for limit in (0, 501):
        outcome = tools.query("SELECT 1", limit=limit)
        refused_before_request(outcome, tools.driver, "INVALID_LIMIT")


@pytest.mark.parametrize("dialect", NEW)
def test_current_state_notes_and_cannot_prove(monkeypatch, dialect):
    tools = make_tools_b(monkeypatch, dialect)
    outcome = tools.query("SELECT 1 AS n")
    v = view(outcome)
    assert outcome.status == "ok", v
    assert v["source_start_at"] is None and v["source_end_at"] is None
    assert re.search(r"\d{4}-\d\d-\d\dT\d\d:\d\d", notes(outcome)[0]), notes(outcome)
    assert isinstance(v["cannot_prove"], str) and v["cannot_prove"]
    assert v["source"] == "otel-demo"


@pytest.mark.parametrize("dialect", NEW)
def test_unconfigured_worker_has_no_sql_tool(monkeypatch, dialect):
    driver = FakeSqlB(dialect)
    install(monkeypatch, driver)
    executor, _ = worker(sql_env(None))
    outcome = executor.execute(call(QUERY, {"sql": "SELECT 1"}))
    assert outcome.status in ("error", "denied")
    assert driver.connections == []
