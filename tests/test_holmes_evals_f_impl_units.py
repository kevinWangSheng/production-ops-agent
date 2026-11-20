"""Implementer unit tests for #666 PR-F (the C6 database questions). The
independent contract tests are the acceptance suite; these pin the
implementer's own choices with synthetic question directories and in-process
doubles. No cluster, no database, no model. Passwords are placeholders.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts.holmes_evals import db_backends, driver, intake, log_backends, questions
from scripts.holmes_evals.setup import ScriptResult
from scripts.itbench_lite.services import Started
from tests.holmes_evals_support import upstream_root

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
Q217 = "217_database_sql_query"
Q218 = "218_mysql_query_performance"
Q223 = "223_sqlserver_query_performance"
Q229 = "229_mongodb_schema_discovery"


class _Pg:
    owner_dsn = "postgresql://owner@127.0.0.1:1/x"
    runtime_dsn = "postgresql://worker@127.0.0.1:1/x"
    web_dsn = "postgresql://web@127.0.0.1:1/x"


def _cfg(tmp_path: Path) -> driver.Config:
    cfg = driver.Config(
        round="f1", upstream=tmp_path / "up", tmp_root=tmp_path / "tmp",
        evidence_root=tmp_path / "ev",
    )  # fmt: skip
    cfg.secrets = {"DEEPSEEK_API_KEY": "model-key-x", "ZAI_API_KEY": "judge-key-x"}
    cfg.k8s = driver.K8sTarget("https://127.0.0.1:29243", str(tmp_path / "ca"), "tok")
    cfg.account = intake.new_account()
    return cfg


# -- question set and spec table ----------------------------------------------------


def test_question_set_and_specs() -> None:
    assert questions.QUESTION_GROUPS["c6"] == questions.C6_QUESTIONS
    assert questions.ALL_QUESTIONS[-7:] == questions.C6_QUESTIONS
    assert driver.resolve_questions("c6") == questions.C6_QUESTIONS
    assert driver.resolve_questions("223") == (Q223,)
    assert {log_backends.backend_of(q) for q in questions.C6_SQL_QUESTIONS} == {"sql"}
    assert {log_backends.backend_of(q) for q in questions.C6_MONGO_QUESTIONS} == {
        "mongodb"
    }
    for qid in questions.C6_QUESTIONS:
        assert qid not in questions.R8_13_SKILL_ASSERTION_DROPPED
        spec = db_backends.DB_QUESTIONS[qid]
        assert log_backends.LOG_QUESTIONS[qid].host_ports == (spec.local_port,)


def test_spec_table_matches_pinned_checkout() -> None:
    root = upstream_root()
    if root is None:
        pytest.skip("pinned upstream checkout not present")
    for qid, spec in db_backends.DB_QUESTIONS.items():
        q = questions.load_question(root, qid)
        [forward] = q.port_forwards or []
        assert (forward["namespace"], forward["service"]) == (
            spec.namespace,
            spec.deployment,
        )
        assert (forward["local_port"], forward["remote_port"]) == (
            spec.local_port,
            spec.remote_port,
        )
        assert q.setup_timeout == spec.setup_timeout_s
        assert q.include_tool_calls is True


# -- product env and isolation ------------------------------------------------------


def test_product_env_per_dialect() -> None:
    env = db_backends.product_env(db_backends.DB_QUESTIONS[Q218], "pw-x")
    assert env == {
        "OPSPILOT_SQL_URL": "mysql://127.0.0.1:10218/testdb",
        "OPSPILOT_SQL_USERNAME": "opspilot_ro",
        "OPSPILOT_SQL_PASSWORD": "pw-x",
        "OPSPILOT_SQL_TLS": "require",
    }
    ch = db_backends.product_env(
        db_backends.DB_QUESTIONS["222_clickhouse_query_performance"], "pw-x"
    )
    assert ch["OPSPILOT_SQL_URL"] == "clickhouse+http://127.0.0.1:10222/default"
    assert ch["OPSPILOT_SQL_TLS"] == "disable"
    mongo = db_backends.product_env(db_backends.DB_QUESTIONS[Q229], "pw-x")
    assert mongo["OPSPILOT_MONGODB_URL"] == "mongodb://127.0.0.1:10227"
    assert mongo["OPSPILOT_MONGODB_DATABASE"] == "shopdb"
    assert mongo["OPSPILOT_MONGODB_AUTH_SOURCE"] == "admin"


def test_web_gets_the_address_only_and_worker_the_account(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    db = db_backends.product_env(db_backends.DB_QUESTIONS[Q217], "pw-x")
    web = driver.web_env(
        cfg, _Pg(), tmp_path / "ids.json", cfg.account, backend="sql", db_env=db
    )
    assert web["OPSPILOT_SQL_URL"] == db["OPSPILOT_SQL_URL"]
    assert "OPSPILOT_SQL_PASSWORD" not in web and "OPSPILOT_ES_URL" not in web
    assert cfg.k8s is not None
    worker = driver.worker_env(
        cfg, _Pg(), {}, cfg.k8s, None, cfg.secrets, backend="sql", db_env=db
    )
    assert worker["OPSPILOT_SQL_PASSWORD"] == "pw-x"
    assert "OPSPILOT_ES_URL" not in worker
    driver.check_env_isolation(web, "", role="web", db_port=10217)
    driver.check_env_isolation(worker, "", role="worker", db_port=10217)


@pytest.mark.parametrize(
    ("role", "env", "port"),
    [
        ("web", {"OPSPILOT_SQL_PASSWORD": "x"}, None),
        ("web", {"OPSPILOT_MONGODB_USERNAME": "x"}, None),
        ("worker", {"MYSQL_ROOT_PASSWORD": "x"}, None),
        ("worker", {"OPSPILOT_SQL_URL": "postgresql://127.0.0.1:5432/x"}, 10217),
        ("worker", {"OPSPILOT_MONGODB_URL": "mongodb://127.0.0.1:27017"}, 10227),
    ],
)
def test_isolation_refuses(role: Any, env: dict[str, str], port: int | None) -> None:
    with pytest.raises(driver.RoundAbort) as err:
        driver.check_env_isolation(env, "", role=role, db_port=port)
    assert err.value.code == "ENV_ISOLATION"


# -- grants: the password never in argv ---------------------------------------------


@pytest.mark.parametrize("qid", sorted(db_backends.DB_QUESTIONS))
def test_grant_scripts_read_the_password_from_stdin(qid: str) -> None:
    spec = db_backends.DB_QUESTIONS[qid]
    script = db_backends.grant_script(spec)
    assert script.startswith("set -e\nread -r PW\n")
    assert db_backends.ACCOUNT in script
    argv = db_backends.provision_argv(spec)
    assert argv[:7] == [
        "kubectl", "exec", "-i", "-n", spec.namespace,
        f"deployment/{spec.deployment}", "--",
    ]  # fmt: skip
    for admin in db_backends.ADMIN_ENV_NAMES:
        # admin access only through the container's variables, never a value
        assert f"{admin}=" not in script.replace(f'"${admin}"', "")


def test_dialect_grants() -> None:
    by = {q: db_backends.grant_script(s) for q, s in db_backends.DB_QUESTIONS.items()}
    assert "GRANT pg_read_all_data, pg_read_all_stats" in by[Q217]
    assert "GRANT SELECT, SHOW VIEW ON testdb.*" in by[Q218]
    assert "GRANT VIEW DEFINITION" in by[Q223]
    assert "<readonly>2</readonly>" in by["222_clickhouse_query_performance"]
    assert '{role: "clusterMonitor", db: "admin"}' in by[Q229]


def test_provision_passes_the_password_on_stdin(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    def run(argv, **kw):
        seen.update(argv=argv, input=kw["input"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(db_backends.subprocess, "run", run)
    db_backends.provision_db_account(db_backends.DB_QUESTIONS[Q218], {}, "secret-pw-x")
    assert seen["input"] == "secret-pw-x\n"
    assert not any("secret-pw-x" in part for part in seen["argv"])
    monkeypatch.setattr(
        db_backends.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", ""),
    )
    with pytest.raises(db_backends.DbSetupError) as err:
        db_backends.provision_db_account(db_backends.DB_QUESTIONS[Q218], {}, "x")
    assert err.value.code == "DB_ACCOUNT_SETUP_FAILED"


def test_precheck_waits_for_login() -> None:
    clock = {"t": 0.0}
    calls = {"n": 0}

    def check(spec, env):
        calls["n"] += 1
        if calls["n"] < 3:
            raise db_backends.DbLoginFailed("x")
        return None

    def sleep(seconds: float) -> None:
        clock["t"] += seconds

    spec = db_backends.DB_QUESTIONS[Q217]
    assert (
        db_backends.precheck_with_wait(
            spec, {}, check, now=lambda: clock["t"], sleep=sleep
        )
        is None
    )
    assert calls["n"] == 3

    def never(spec, env):
        raise db_backends.DbLoginFailed("x")

    with pytest.raises(db_backends.DbLoginFailed):
        db_backends.precheck_with_wait(
            spec, {}, never, now=lambda: clock["t"], sleep=sleep
        )


def test_precheck_uses_the_products_own_decision() -> None:
    from opspilot.tools import mongodb_query, sql_query

    assert sql_query.session_problem("mysql", [("GRANT INSERT ON testdb.* TO x",)])
    assert sql_query.session_problem("postgresql", []) is None
    status = {"authInfo": {"authenticatedUsers": [{"user": "u"}],
              "authenticatedUserRoles": [{"role": "root", "db": "admin"}]}}  # fmt: skip
    assert mongodb_query.account_problem(status)


# -- one attempt with doubles -----------------------------------------------------------


def _question(root: Path, qid: str) -> None:
    spec = db_backends.DB_QUESTIONS[qid]
    d = root / questions.QUESTIONS_DIR / qid
    d.mkdir(parents=True)
    (d / "test_case.yaml").write_text(
        json.dumps(
            {
                "user_prompt": "List the tables and find the slow query.",
                "before_test": "kubectl apply -f db.yaml",
                "after_test": "kubectl delete namespace x",
                "setup_timeout": spec.setup_timeout_s,
                "include_tool_calls": True,
                "port_forwards": [
                    {
                        "namespace": spec.namespace,
                        "service": spec.deployment,
                        "local_port": spec.local_port,
                        "remote_port": spec.remote_port,
                    }
                ],  # fmt: skip
                "expected_output": ["SYNTHETIC-ANSWER-ELEMENT"],
            }
        )
    )


def _report() -> dict[str, Any]:
    return {
        "schema_version": "m0-report-v2",
        "assessment_status": "completed",
        "conclusion": "supported",
        "summary": "orders has no index on customer_id.",
        "claims": [
            {"kind": "fact", "text": "seq scan", "evidence_ids": ["ev-1"],
             "target_refs": ["t-1"], "time_scope_ref": "w-1"}
        ],
        "gaps": [],
        "next_steps": [],
    }  # fmt: skip


def _backends(seen: dict[str, Any], *, precheck: str | None = None) -> driver.Backends:
    order: list[str] = seen.setdefault("order", [])

    def run_script(script: str | None, **kw: Any) -> ScriptResult:
        order.append(kw["operation"])
        stamp = NOW.isoformat()
        return ScriptResult(kw["operation"], 0, False, stamp, stamp, 0.1, "0" * 64)

    def start(name: str) -> Any:
        def started(*a: Any, **k: Any) -> Started:
            order.append(name)
            seen["env_" + name] = a[0]
            return Started(name, lambda: order.append("stop-" + name))

        return started

    def forward(f: Any, env: Any, log: Any) -> Started:
        order.append("port_forward")
        return Started("pf", lambda: order.append("stop_port_forward"))

    def provision(spec: Any, env: Any, password: str) -> None:
        order.append("provision_db_account")
        seen["password"] = password

    def check(spec: Any, env: Any) -> str | None:
        order.append("db_precheck")
        return precheck

    def submit(web_url: str, account: Any, **kw: Any) -> intake.SubmitResult:
        order.append("submit")
        return intake.SubmitResult(
            201, "inc-1", "00000000-0000-0000-0000-000000000001", False, None
        )

    def judge(job: Any) -> int:
        Path(job["out"]).write_text(json.dumps({"choice": "A", "passed": True,
            "error_code": None, "attempts": 1, "prompt_sha256": "p", "request_bytes": 9}))  # fmt: skip
        return 0

    return driver.Backends(
        start_pg=lambda d, p: _Pg(),
        stop_pg=lambda pg: None,
        start_web=start("web"),
        start_worker=start("worker"),
        run_script=run_script,
        submit=submit,
        run_state=lambda dsn, run: "completed",
        collect=lambda dsn, run: (
            {"report": _report(), "outcome": {"report_available": True}},
            {"r": 1},
        ),
        read_tool_calls=lambda dsn, run: [],
        trace_roots=lambda p, r, e: {"project": None, "read_back": "x", "roots": []},
        judge=judge,
        port_in_use=lambda port: False,
        namespace_gone=lambda ns, env: True,
        start_port_forward=forward,
        provision_db_account=provision,
        db_precheck=check,
        now=lambda: NOW,
        sleep=lambda s: None,
    )


def test_a_c6_trial_provisions_checks_and_delivers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPSPILOT_LEDGER_DIR", str(tmp_path / "ledger"))
    cfg = _cfg(tmp_path)
    cfg.questions = (Q218,)
    _question(cfg.upstream, Q218)
    seen: dict[str, Any] = {}
    out = driver.run_question(cfg, _backends(seen), Q218, [1])
    assert out["trials"][1]["status"] == "completed", out
    order = seen["order"]
    assert order.index("before_test") < order.index("port_forward")
    assert order.index("port_forward") < order.index("provision_db_account")
    assert order.index("provision_db_account") < order.index("db_precheck")
    assert order.index("db_precheck") < order.index("web") < order.index("worker")
    assert order.index("submit") < order.index("stop_port_forward")
    assert seen["env_worker"]["OPSPILOT_SQL_PASSWORD"] == seen["password"]
    assert "OPSPILOT_SQL_PASSWORD" not in seen["env_web"]
    raw = cfg.summary_path(Q218, 1).read_text()
    assert seen["password"] not in raw
    block = json.loads(raw)["holmes_eval"]
    assert block["db_account"]["precheck"] == "pass"
    assert block["db_account"]["grants"] == "C6-21:mysql"
    assert block["data_plane"]["mode"] == "port-forward"
    assert block["data_plane"]["tls"] == "require"
    assert block["shift"]["reason"] == "NO_TIME_REFERENCE"
    assert {"DB_READ_ONLY_ACCOUNT", "DB_TOOL_SURFACE_DIFFERS"} <= set(
        block["disclosures"]
    )


def test_a_refused_precheck_is_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPSPILOT_LEDGER_DIR", str(tmp_path / "ledger"))
    cfg = _cfg(tmp_path)
    cfg.questions = (Q217,)
    _question(cfg.upstream, Q217)
    seen: dict[str, Any] = {}
    out = driver.run_question(
        cfg, _backends(seen, precheck="SQL_ACCOUNT_NOT_READ_ONLY: x"), Q217, [1]
    )
    trial = out["trials"][1]
    assert trial["status"] != "completed", out
    assert seen["order"].count("provision_db_account") == 1  # not retried
    assert "submit" not in seen["order"]


def test_port_conflict_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSPILOT_LEDGER_DIR", str(tmp_path / "ledger"))
    cfg = _cfg(tmp_path)
    cfg.ports = driver.Ports(pg=10217)
    cfg.questions = (Q217,)
    _question(cfg.upstream, Q217)
    with pytest.raises(driver.RoundAbort) as err:
        driver.run_question(cfg, _backends({}), Q217, [1])
    assert err.value.code == "DB_PORT_CONFLICT"


def test_dry_run_plan(tmp_path: Path) -> None:
    plan = driver.dry_run_plan(_cfg(tmp_path), Q223)
    assert plan["backend"] == "sql" and plan["grants"] == "C6-21:mssql"
    assert plan["product_env"]["OPSPILOT_SQL_PASSWORD"] == "<random per attempt>"
    assert plan["flow"][5:7] == ["provision_db_account", "db_precheck"]


_CLIENTS = ("psql", "mysql", "mariadb", "sqlcmd", "mongosh", "clickhouse-client")


@pytest.mark.parametrize("qid", sorted(db_backends.DB_QUESTIONS))
def test_grant_script_keeps_the_password_out_of_client_argv(
    tmp_path: Path, qid: str
) -> None:
    """Run each grant script under sh with fake clients that log their argv
    (review of #756: PG once passed the password with ``-v pw=``)."""
    bin_dir, log = tmp_path / "bin", tmp_path / "argv.log"
    bin_dir.mkdir()
    for name in _CLIENTS:
        fake = bin_dir / name
        fake.write_text(f'#!/bin/sh\necho "{name}: $*" >> "{log}"\ncat > /dev/null\n')
        fake.chmod(0o755)
    users_d = tmp_path / "users.d"
    users_d.mkdir()
    script = (
        db_backends.grant_script(db_backends.DB_QUESTIONS[qid])
        .replace("/opt/mssql-tools18/bin/sqlcmd", "sqlcmd")
        .replace("/etc/clickhouse-server/users.d", str(users_d))
    )
    password = "pw" + "Q7xZ" * 6
    admin_env = {n: "admin-x" for n in db_backends.ADMIN_ENV_NAMES}
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "POSTGRES_USER": "postgres",
           "MONGO_INITDB_ROOT_USERNAME": "root", **admin_env}  # fmt: skip
    result = subprocess.run(
        ["sh", "-c", script], input=password + "\n", env=env,
        capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    logged = log.read_text() if log.exists() else ""
    if db_backends.DB_QUESTIONS[qid].dialect != "clickhouse":
        assert logged, "the client was not invoked"
    assert password not in logged
