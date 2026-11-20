"""#666 PR-F contract: the C6 question lists, the per-question database
table, product environments, isolation, the grant scripts, the default
account provisioning (password on stdin only) and the default pre-check
(the product's own self-check), without a running stack.

Independent contract tests written from the task record's "PR-F 接口"
section (docs/tasks/2026-11-18-holmes-evals-666.md) without reading the
PR-F implementation. No database, kubectl, kind, docker, model or network:
``kubectl`` is a stand-in shell script on ``PATH``; database connections are
stand-ins behind the product's ``default_connector``.

Rules under test:

* ``C6_SQL_QUESTIONS`` (5), ``C6_MONGO_QUESTIONS`` (2), ``C6_QUESTIONS``;
  ``ALL_QUESTIONS`` ends with them; group ``c6``; no R8-13 member;
* ``db_backends.DbQuestionSpec`` (frozen) and ``DB_QUESTIONS`` values, and
  against the pinned upstream checkout when it is present;
* ``log_backends.backend_of`` -> ``sql`` / ``mongodb``;
* ``Ports.log_ports`` empty, ``Config.log_mode`` ``none``;
* ``log_setup_env`` adds only ``KUBECONFIG``;
* ``shift_question`` -> ``NO_TIME_REFERENCE``;
* ``tool_env``/``web_env``/``worker_env`` for ``sql``/``mongodb`` with
  ``db_env``; earlier backends byte-identical with ``db_env=None``;
* ``check_env_isolation``'s three new conditions;
* ``grant_script(spec)`` per dialect: the C6-21 grants, admin from the
  container's own variables, no password literal;
* the default ``provision_db_account``: argv without the password, stdin
  with it; non-zero exit -> ``DB_ACCOUNT_SETUP_FAILED``;
* the default ``db_precheck``: allow-listed grants -> None, others -> the
  product's own refusal note.

Assumptions beyond the interface (also marked ``# ASSUMPTION:``):

* the default ``provision_db_account``/``db_precheck`` are the default
  values of the new ``Backends`` fields;
* ``check_env_isolation`` learns the question's ``local_port`` through a new
  keyword; the test looks for one of a few plausible names (the interface
  does not name it -- reported);
* ``db_env`` given to ``tool_env``/``web_env``/``worker_env`` is the full
  product mapping above (``holmes_evals_f_support.product_env``).
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.holmes_evals_b_support import ENGINEER, no_proxy_env  # noqa: F401
from tests.holmes_evals_c_support import find
from tests.holmes_evals_f_support import (
    ADMIN_VARS,
    C6_ALL,
    C6_MONGO,
    C6_SQL,
    FAKE_ADMIN_PW,
    FAKE_DB_PW,
    GRANT_ROWS,
    Q217,
    Q218,
    Q219,
    Q222,
    Q223,
    Q229,
    Q230,
    RO_USER,
    TABLE,
    FakeMongoConnection,
    FakeSqlConnection,
    mongo_status,
    patch_connectors,
    prefix,
    product_env,
    url_key,
)
from tests.holmes_evals_support import mod, upstream_root

pytestmark = pytest.mark.usefixtures("no_proxy_env")

BASE = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/x",
    "TMPDIR": "/tmp/x",
    "LANG": "C.UTF-8",
    "LC_ALL": "C",
}
NOISY = {
    **BASE,
    "TZ": "Asia/Shanghai",
    "ELASTICSEARCH_URL": "http://127.0.0.1:9200",
    "ELASTICSEARCH_API_KEY": ENGINEER,
    "DEEPSEEK_API_KEY": "model-fake-value-4",
    "OPSPILOT_SQL_PASSWORD": FAKE_DB_PW,
    "MYSQL_ROOT_PASSWORD": FAKE_ADMIN_PW,
}


def _db() -> Any:
    return mod("db_backends")


def _spec(qid: str) -> Any:
    return _db().DB_QUESTIONS[qid]


# -- question lists ------------------------------------------------------------------


def test_c6_question_lists() -> None:
    q = mod("questions")
    assert tuple(q.C6_SQL_QUESTIONS) == C6_SQL
    assert tuple(q.C6_MONGO_QUESTIONS) == C6_MONGO
    assert tuple(q.C6_QUESTIONS) == C6_ALL
    assert tuple(q.ALL_QUESTIONS)[-7:] == C6_ALL
    assert tuple(q.ALL_QUESTIONS) == (
        tuple(q.B1_QUESTIONS) + tuple(q.C5_QUESTIONS) + tuple(q.C2_QUESTIONS)
        + tuple(q.C1_QUESTIONS) + C6_ALL
    )  # fmt: skip
    assert tuple(q.QUESTION_GROUPS["c6"]) == C6_ALL
    before = set(q.ALL_QUESTIONS[:-7])
    assert not before & set(C6_ALL)
    assert len(set(q.ALL_QUESTIONS)) == len(q.ALL_QUESTIONS)


def test_c6_not_r8_13() -> None:
    q = mod("questions")
    assert not set(q.R8_13_SKILL_ASSERTION_DROPPED) & set(C6_ALL)
    assert not set(q.SKILL_ASSERTION_DROPPED) & set(C6_ALL)


def test_resolve_c6_group_and_numbers() -> None:
    driver = mod("driver")
    assert driver.resolve_questions("c6") == C6_ALL
    assert driver.resolve_questions("217") == (Q217,)
    assert driver.resolve_questions("229,230") == (Q229, Q230)
    assert driver.resolve_questions(f"{Q223},275") == (
        Q223,
        "275_es_field_name_correction",
    )


# -- per-question table --------------------------------------------------------------


@pytest.mark.parametrize("qid", C6_ALL)
def test_db_question_spec(qid: str) -> None:
    db = _db()
    row = TABLE[qid]
    spec = db.DB_QUESTIONS[qid]
    assert isinstance(spec, db.DbQuestionSpec)
    assert spec.qid == qid
    assert spec.backend == row.backend
    assert spec.dialect == row.dialect
    assert spec.namespace == row.namespace
    assert spec.deployment == row.deployment
    assert spec.local_port == row.local_port
    assert spec.remote_port == row.remote_port
    assert spec.database == row.database
    assert tuple(spec.host_ports) == (row.local_port,)
    assert spec.setup_timeout_s == row.setup_timeout_s
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.qid = "x"  # type: ignore[misc]


def test_db_questions_cover_exactly_c6() -> None:
    assert set(_db().DB_QUESTIONS) == set(C6_ALL)


@pytest.mark.parametrize("qid", C6_ALL)
def test_db_question_spec_matches_pinned_upstream(qid: str) -> None:
    root = upstream_root()
    if root is None:
        pytest.skip("pinned upstream checkout not present")
    # Through the project's whitelisted loader only (no expected_output).
    question = mod("questions").load_question(root, qid)
    spec = _spec(qid)
    (forward,) = question.port_forwards
    assert forward["namespace"] == spec.namespace
    assert forward["service"] == spec.deployment
    assert int(forward["local_port"]) == spec.local_port
    assert int(forward["remote_port"]) == spec.remote_port
    assert question.setup_timeout == spec.setup_timeout_s
    assert question.include_tool_calls is True
    assert not question.memories_generated


def test_backend_of() -> None:
    lb, q = mod("log_backends"), mod("questions")
    for qid in C6_SQL:
        assert lb.backend_of(qid) == "sql"
    for qid in C6_MONGO:
        assert lb.backend_of(qid) == "mongodb"
    for qid in q.B1_QUESTIONS:
        assert lb.backend_of(qid) == "elasticsearch"


def test_ports_and_log_mode(tmp_path: Path) -> None:
    driver = mod("driver")
    ports = driver.Ports()
    for backend in ("sql", "mongodb"):
        for mode in ("fake", "gateway", "docker", "none"):
            assert dict(ports.log_ports(backend, mode)) == {}
    cfg = driver.Config(round="t1", upstream=tmp_path)
    assert cfg.log_mode("sql") == "none"
    assert cfg.log_mode("mongodb") == "none"


def _base(environ: dict[str, str]) -> dict[str, str]:
    return {
        k: v
        for k, v in environ.items()
        if k in {"PATH", "HOME", "TMPDIR", "LANG", "TZ"} or k.startswith("LC_")
    }


@pytest.mark.parametrize("qid", [Q217, Q229])
def test_log_setup_env_only_kubeconfig(tmp_path: Path, qid: str) -> None:
    fn = find("log_setup_env", "setup", "log_backends", "driver")
    spec = _spec(qid)
    env = fn(spec, victorialogs_url="http://127.0.0.1:29428", kubeconfig=None,
             environ=NOISY)  # fmt: skip
    assert env == _base(NOISY)
    kube = tmp_path / "admin.kubeconfig"
    env = fn(spec, victorialogs_url="http://127.0.0.1:29428", kubeconfig=kube,
             environ=NOISY)  # fmt: skip
    assert env == _base(NOISY) | {"KUBECONFIG": str(kube)}
    assert FAKE_DB_PW not in json.dumps(env) and FAKE_ADMIN_PW not in json.dumps(env)


@pytest.mark.parametrize("backend", ["sql", "mongodb"])
def test_shift_question_no_time_reference(backend: str) -> None:
    from datetime import UTC, datetime

    shift = mod("shift")
    qid = Q217 if backend == "sql" else Q229
    text = "List the tables and tell me the verification code."
    now = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
    delivered, record = shift.shift_question(
        backend, es_admin=None, loki_admin=None, qid=qid, question=text,
        now=now, setup_started=now,
    )  # fmt: skip
    assert delivered == text
    assert isinstance(record, shift.ShiftRecord)
    assert record.applied is False
    assert record.reason == "NO_TIME_REFERENCE"
    assert record.question_rewrites == []
    assert record.as_text_shift() is None


# -- environments ----------------------------------------------------------------------


def _cfg(tmp_path: Path, **overrides: Any) -> Any:
    driver = mod("driver")
    wanted: dict[str, Any] = {
        "round": "t1",
        "upstream": tmp_path / "upstream",
        "questions": ("275_es_field_name_correction",),
        "es_mode": "fake",
        "es_url": "http://127.0.0.1:29292",
        "trace": "off",
        "judge": False,
        "allow_dirty": True,
        "tmp_root": tmp_path / "tmp",
        "evidence_root": tmp_path / "evidence",
        **overrides,
    }
    return driver.Config(**wanted)


def _pg() -> SimpleNamespace:
    return SimpleNamespace(
        owner_dsn="postgresql://owner:pw@127.0.0.1:29210/opspilot",
        runtime_dsn="postgresql://eval_worker:pw@127.0.0.1:29210/opspilot",
        web_dsn="postgresql://eval_web:pw@127.0.0.1:29210/opspilot",
        port=29210,
    )


def _k8s(tmp_path: Path) -> Any:
    ca = tmp_path / "ca.pem"
    ca.write_text("not a real ca\n")
    return mod("driver").K8sTarget(
        api_url="https://127.0.0.1:29243",
        ca_file=ca,
        token="k8s-fake-value-6",
        namespace="otel-demo",
    )


@pytest.fixture
def dirty_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in NOISY.items():
        if key not in BASE:
            monkeypatch.setenv(key, value)


def _env_trio(tmp_path: Path, backend: str, db_env: Any, **kw: Any) -> tuple:
    driver, intake = mod("driver"), mod("intake")
    cfg = _cfg(tmp_path)
    account = intake.new_account()
    t = driver.tool_env(cfg, backend, db_env=db_env, **kw)
    w = driver.web_env(cfg, _pg(), tmp_path / "ids.json", account, backend=backend,
                       db_env=db_env, **kw)  # fmt: skip
    k = driver.worker_env(
        cfg, _pg(), {"OPSPILOT_ES_API_KEY": "ro-3"}, _k8s(tmp_path), None,
        {"DEEPSEEK_API_KEY": "m-4"}, backend=backend, db_env=db_env, **kw,
    )  # fmt: skip
    return t, w, k, account


_OTHER_DATA_PLANES = (
    "OPSPILOT_ES_",
    "ELASTICSEARCH_",
    "OPSPILOT_LOKI_",
    "OPSPILOT_VICTORIALOGS_",
)


@pytest.mark.parametrize("qid", C6_ALL)
def test_db_question_envs(tmp_path: Path, dirty_environ: None, qid: str) -> None:
    driver = mod("driver")
    backend = TABLE[qid].backend
    db_env = product_env(qid, FAKE_DB_PW)
    t, web, worker, _ = _env_trio(tmp_path, backend, db_env)
    other = "OPSPILOT_SQL_" if backend == "mongodb" else "OPSPILOT_MONGODB_"
    for env in (t, web, worker):
        assert not [k for k in env if k.startswith(_OTHER_DATA_PLANES)], env.keys()
        assert not [k for k in env if k.startswith(other)]
        assert "VICTORIALOGS_URL" not in env
        assert env[url_key(qid)] == db_env[url_key(qid)]
        for name in ADMIN_VARS:
            assert name not in env
    # The web (and the shared tool profile) get the address alone.
    assert [k for k in web if k.startswith(prefix(qid))] == [url_key(qid)]
    assert [k for k in t if k.startswith(prefix(qid))] == [url_key(qid)]
    assert FAKE_DB_PW not in json.dumps(web) and FAKE_DB_PW not in json.dumps(t)
    # The worker gets exactly the mapping the interface lists.
    assert {k: v for k, v in worker.items() if k.startswith(prefix(qid))} == db_env
    assert worker["OPSPILOT_WORKER_CAPACITY"] == "1"
    assert worker["DEEPSEEK_API_KEY"] == "m-4"
    assert worker["OPSPILOT_K8S_NAMESPACE"] == "otel-demo"
    driver.check_env_isolation(web, ENGINEER, role="web")
    _isolation(driver, worker, qid)


@pytest.mark.parametrize(
    ("qid", "tls"),
    [(Q217, "disable"), (Q218, "require"), (Q219, "disable"), (Q222, "disable"),
     (Q223, "disable")],
)  # fmt: skip
def test_sql_url_scheme_and_tls(qid: str, tls: str) -> None:
    env = product_env(qid, FAKE_DB_PW)
    # The product parses the address the driver hands it to its dialect.
    from opspilot.tools.sql_query import sql_dialect

    expected = {"mariadb": "mysql"}.get(TABLE[qid].dialect, TABLE[qid].dialect)
    assert sql_dialect(env) == expected
    assert env["OPSPILOT_SQL_TLS"] == tls


def test_earlier_backends_byte_identical_with_db_env_none(
    tmp_path: Path, dirty_environ: None
) -> None:
    driver, intake = mod("driver"), mod("intake")
    cfg = _cfg(tmp_path)
    account = intake.new_account()
    log_env = {"OPSPILOT_LOKI_URL": "http://127.0.0.1:29231",
               "OPSPILOT_LOKI_USERNAME": "u", "OPSPILOT_LOKI_PASSWORD": "p-1"}  # fmt: skip
    for backend in ("elasticsearch", "loki", "victorialogs", "kubernetes"):
        assert driver.tool_env(cfg, backend) == driver.tool_env(
            cfg, backend, db_env=None
        )
        w0 = driver.web_env(cfg, _pg(), tmp_path / "i.json", account, backend=backend)
        w1 = driver.web_env(
            cfg, _pg(), tmp_path / "i.json", account, backend=backend, db_env=None
        )
        # OPSPILOT_UI_USERS is salted per call (PR-B).
        w0.pop("OPSPILOT_UI_USERS"), w1.pop("OPSPILOT_UI_USERS")
        assert json.dumps(w0, sort_keys=True) == json.dumps(w1, sort_keys=True)
        args = (cfg, _pg(), {"OPSPILOT_ES_API_KEY": "ro-3"}, _k8s(tmp_path), None,
                {"DEEPSEEK_API_KEY": "m-4"})  # fmt: skip
        k0 = driver.worker_env(*args, backend=backend, log_env=log_env)
        k1 = driver.worker_env(*args, backend=backend, log_env=log_env, db_env=None)
        assert json.dumps(k0, sort_keys=True) == json.dumps(k1, sort_keys=True)
        for env in (w0, k0):
            assert not [k for k in env if k.startswith(("OPSPILOT_SQL_",
                                                        "OPSPILOT_MONGODB_"))]  # fmt: skip


# -- isolation -------------------------------------------------------------------------

_PORT_KWARGS = ("db_spec", "spec", "question_port", "db_port", "db_local_port",
                "local_port")  # fmt: skip


def _port_kwarg(driver: Any, qid: str) -> dict[str, Any]:
    # ASSUMPTION: the interface does not say how check_env_isolation learns
    # the question's local_port; accept the plausible keyword names.
    names = inspect.signature(driver.check_env_isolation).parameters
    for name in _PORT_KWARGS:
        if name in names:
            spec = _spec(qid)
            return {name: spec if name in ("db_spec", "spec") else spec.local_port}
    return {}


def _isolation(driver: Any, env: dict[str, str], qid: str) -> None:
    driver.check_env_isolation(env, ENGINEER, role="worker", **_port_kwarg(driver, qid))


@pytest.mark.parametrize(
    "extra",
    [
        {"OPSPILOT_SQL_USERNAME": RO_USER},
        {"OPSPILOT_SQL_PASSWORD": FAKE_DB_PW},
        {"OPSPILOT_MONGODB_USERNAME": RO_USER},
        {"OPSPILOT_MONGODB_PASSWORD": FAKE_DB_PW},
    ],
)
def test_web_with_db_credentials_is_env_isolation(extra: dict[str, str]) -> None:
    driver = mod("driver")
    env = {**BASE, "OPSPILOT_BIND": "127.0.0.1:29280",
           "OPSPILOT_SQL_URL": "postgresql://127.0.0.1:10217/holmesdb", **extra}  # fmt: skip
    with pytest.raises(driver.RoundAbort) as caught:
        driver.check_env_isolation(env, ENGINEER, role="web")
    assert caught.value.code == "ENV_ISOLATION"
    assert FAKE_DB_PW not in str(caught.value)
    assert FAKE_DB_PW not in str(getattr(caught.value, "detail", ""))


@pytest.mark.parametrize("role", ["web", "worker"])
@pytest.mark.parametrize("name", ADMIN_VARS)
def test_admin_variable_in_any_process_is_env_isolation(role: str, name: str) -> None:
    driver = mod("driver")
    env = {**BASE, name: FAKE_ADMIN_PW}
    if role == "worker":
        env |= product_env(Q217, FAKE_DB_PW)
    with pytest.raises(driver.RoundAbort) as caught:
        driver.check_env_isolation(
            env, ENGINEER, role=role,
            **(_port_kwarg(driver, Q217) if role == "worker" else {}),
        )  # fmt: skip
    assert caught.value.code == "ENV_ISOLATION"
    assert FAKE_ADMIN_PW not in str(caught.value)
    assert FAKE_ADMIN_PW not in str(getattr(caught.value, "detail", ""))


@pytest.mark.parametrize("qid", [Q217, Q229])
def test_worker_db_url_on_another_port_is_env_isolation(qid: str) -> None:
    driver = mod("driver")
    kw = _port_kwarg(driver, qid)
    assert kw, (
        "check_env_isolation has no keyword for the question's local_port "
        f"(looked for {_PORT_KWARGS}); the interface does not name it"
    )
    env = {**BASE, **product_env(qid, FAKE_DB_PW)}
    driver.check_env_isolation(env, ENGINEER, role="worker", **kw)
    key = url_key(qid)
    env[key] = env[key].replace(str(TABLE[qid].local_port), "29210")
    with pytest.raises(driver.RoundAbort) as caught:
        driver.check_env_isolation(env, ENGINEER, role="worker", **kw)
    assert caught.value.code == "ENV_ISOLATION"
    assert FAKE_DB_PW not in str(caught.value)


def test_worker_with_db_credentials_is_allowed() -> None:
    driver = mod("driver")
    for qid in C6_ALL:
        env = {**BASE, **product_env(qid, FAKE_DB_PW), "DEEPSEEK_API_KEY": "m-4"}
        _isolation(driver, env, qid)
        web = {**BASE, url_key(qid): product_env(qid, FAKE_DB_PW)[url_key(qid)]}
        driver.check_env_isolation(web, ENGINEER, role="web")


# -- grant scripts -----------------------------------------------------------------------


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).lower()


def _has(text: str, *fragments: str) -> None:
    flat = _norm(text)
    for fragment in fragments:
        assert _norm(fragment) in flat, fragment


def _var(text: str, name: str) -> bool:
    return re.search(r"\$\{?" + name + r"\b", text) is not None


_WRITE = re.compile(
    r"\b(insert|update|delete|truncate|all privileges|with grant option|"
    r"super|dbowner|db_owner|db_datawriter|readwrite|dbadmin)\b",
    re.I,
)


def _no_write_grants(text: str) -> None:
    # Only statements that grant or create roles may mention privileges.
    for stmt in re.split(r"[;\n]", text):
        if re.search(r"\bgrant\b|\broles\b|\bcreateuser\b", stmt, re.I):
            bad = _WRITE.findall(stmt)
            assert not bad, (bad, stmt)


@pytest.mark.parametrize("qid", C6_ALL)
def test_grant_script_is_fixed_and_has_no_password(qid: str) -> None:
    db = _db()
    spec = _spec(qid)
    text = db.grant_script(spec)
    assert isinstance(text, str) and text.strip()
    assert db.grant_script(spec) == text  # fixed text, no per-call value
    assert RO_USER in text
    for literal in (FAKE_DB_PW, FAKE_ADMIN_PW):
        assert literal not in text
    _no_write_grants(text)


def test_grant_script_postgresql() -> None:
    text = _db().grant_script(_spec(Q217))
    _has(text, "CREATE ROLE opspilot_ro LOGIN PASSWORD",
         "GRANT CONNECT ON DATABASE holmesdb", "pg_read_all_data",
         "pg_read_all_stats", "default_transaction_read_only")  # fmt: skip
    assert re.search(r"default_transaction_read_only\s*(=|to)\s*'?on", text, re.I)
    assert _var(text, "POSTGRES_USER")


@pytest.mark.parametrize(("qid", "admin"), [(Q218, "MYSQL_ROOT_PASSWORD"),
                                            (Q219, "MARIADB_ROOT_PASSWORD")])  # fmt: skip
def test_grant_script_mysql_family(qid: str, admin: str) -> None:
    text = _db().grant_script(_spec(qid))
    _has(text, "CREATE USER 'opspilot_ro'@'%' IDENTIFIED BY",
         "GRANT SELECT, SHOW VIEW ON testdb.*",
         "GRANT SELECT ON performance_schema.*", "GRANT PROCESS ON *.*")  # fmt: skip
    assert _var(text, admin)


def test_grant_script_clickhouse() -> None:
    text = _db().grant_script(_spec(Q222))
    _has(text, "/etc/clickhouse-server/users.d/opspilot_ro.xml",
         "password_sha256_hex", "GRANT SELECT ON default.*",
         "GRANT SELECT ON system.*")  # fmt: skip
    assert re.search(r"<readonly>\s*2\s*</readonly>|readonly\s*=\s*2", text)
    assert "CREATE USER" not in text.upper()


def test_grant_script_mssql() -> None:
    text = _db().grant_script(_spec(Q223))
    _has(text, "CREATE LOGIN opspilot_ro WITH PASSWORD", "CHECK_POLICY = OFF",
         "CREATE USER opspilot_ro FOR LOGIN opspilot_ro",
         "ALTER ROLE db_datareader ADD MEMBER opspilot_ro", "GRANT SHOWPLAN",
         "GRANT VIEW DATABASE STATE", "GRANT VIEW DEFINITION",
         "GRANT VIEW SERVER PERFORMANCE STATE")  # fmt: skip
    assert _var(text, "MSSQL_SA_PASSWORD")


@pytest.mark.parametrize(("qid", "database"), [(Q229, "shopdb"), (Q230, "opsdb")])
def test_grant_script_mongodb(qid: str, database: str) -> None:
    text = _db().grant_script(_spec(qid))
    _has(text, "createUser")
    flat = re.sub(r"[\s'\"]", "", text)
    assert "user:opspilot_ro" in flat
    assert f"role:read,db:{database}" in flat
    assert "role:clusterMonitor,db:admin" in flat
    assert _var(text, "MONGO_INITDB_ROOT_USERNAME")
    assert _var(text, "MONGO_INITDB_ROOT_PASSWORD")


# -- default provisioning: kubectl stand-in ----------------------------------------------


def _fake_kubectl(tmp_path: Path, exit_code: int = 0) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    record = tmp_path / "kubectl-record"
    record.mkdir(exist_ok=True)
    script = bin_dir / "kubectl"
    script.write_text(
        "#!/bin/sh\n"
        f'n=$(ls "{record}" | wc -l | tr -d " ")\n'
        f'd="{record}/$n"\n'
        'mkdir -p "$d"\n'
        'for a in "$@"; do printf "%s\\0" "$a"; done > "$d/argv"\n'
        'printf "%s" "${KUBECONFIG:-}" > "$d/kubeconfig"\n'
        'cat > "$d/stdin"\n'
        f"exit {exit_code}\n"
    )
    script.chmod(0o755)
    return bin_dir, record


def _calls(record: Path) -> list[dict[str, Any]]:
    out = []
    for d in sorted(record.iterdir(), key=lambda p: int(p.name)):
        argv = (d / "argv").read_bytes().split(b"\0")[:-1]
        out.append({
            "argv": [a.decode() for a in argv],
            "stdin": (d / "stdin").read_bytes().decode(),
            "kubeconfig": (d / "kubeconfig").read_text(),
        })  # fmt: skip
    return out


def _provision_env(tmp_path: Path, bin_dir: Path) -> dict[str, str]:
    kube = tmp_path / "engineer.kubeconfig"
    kube.write_text("apiVersion: v1\nkind: Config\n")
    return {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path),
            "LANG": "C.UTF-8", "KUBECONFIG": str(kube)}  # fmt: skip


@pytest.mark.parametrize("qid", [Q217, Q218, Q223, Q229])
def test_default_provision_password_on_stdin_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qid: str
) -> None:
    driver = mod("driver")
    bin_dir, record = _fake_kubectl(tmp_path)
    env = _provision_env(tmp_path, bin_dir)
    monkeypatch.setenv("PATH", env["PATH"])
    spec = _spec(qid)
    provision = driver.Backends().provision_db_account  # ASSUMPTION: default
    assert provision(spec, env, FAKE_DB_PW) is None
    calls = _calls(record)
    assert calls, "kubectl was not called"
    execs = [c for c in calls if "exec" in c["argv"]]
    assert execs, [c["argv"] for c in calls]
    for call in calls:
        assert FAKE_DB_PW not in "\0".join(call["argv"])
    main = execs[0]
    argv = main["argv"]
    assert "-i" in argv or "--stdin" in argv
    assert argv[argv.index("-n") + 1] == spec.namespace
    assert f"deployment/{spec.deployment}" in argv
    assert "sh" in argv and "-c" in argv
    assert _norm(_db().grant_script(spec)) in _norm(" ".join(argv))
    assert FAKE_DB_PW in main["stdin"]
    assert main["kubeconfig"] == env["KUBECONFIG"]


def test_default_provision_failure_is_db_account_setup_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = mod("driver")
    bin_dir, _ = _fake_kubectl(tmp_path, exit_code=1)
    env = _provision_env(tmp_path, bin_dir)
    monkeypatch.setenv("PATH", env["PATH"])
    with pytest.raises(Exception) as caught:
        driver.Backends().provision_db_account(_spec(Q217), env, FAKE_DB_PW)
    assert getattr(caught.value, "code", None) == "DB_ACCOUNT_SETUP_FAILED"
    assert FAKE_DB_PW not in str(caught.value)
    assert FAKE_DB_PW not in str(getattr(caught.value, "detail", ""))


# -- default pre-check: the product's own self-check -------------------------------------


@pytest.mark.parametrize("qid", C6_SQL)
@pytest.mark.parametrize("read_only", [True, False])
def test_default_precheck_sql(
    monkeypatch: pytest.MonkeyPatch, qid: str, read_only: bool
) -> None:
    from opspilot.tools.sql_query import session_problem

    driver = mod("driver")
    dialect = TABLE[qid].dialect
    ok, bad = GRANT_ROWS[dialect]
    rows = ok if read_only else bad
    seen: list[Any] = []

    def connector(config: Any, timeout: float) -> Any:
        seen.append(config)
        return FakeSqlConnection(dialect, rows)

    patch_connectors(monkeypatch, sql=connector)
    env = product_env(qid, FAKE_DB_PW)
    note = driver.Backends().db_precheck(_spec(qid), env)
    product_dialect = {"mariadb": "mysql"}.get(dialect, dialect)
    if read_only:
        assert note is None
    else:
        assert note == session_problem(product_dialect, rows)
        assert note.startswith("SQL_ACCOUNT_NOT_READ_ONLY")
    assert seen, "the pre-check did not connect"
    config = seen[0]
    assert config.username == RO_USER and config.password == FAKE_DB_PW
    assert config.port == TABLE[qid].local_port
    assert config.host in ("127.0.0.1", "localhost")


@pytest.mark.parametrize("qid", C6_MONGO)
@pytest.mark.parametrize("read_only", [True, False])
def test_default_precheck_mongodb(
    monkeypatch: pytest.MonkeyPatch, qid: str, read_only: bool
) -> None:
    from opspilot.tools.mongodb_query import account_problem

    driver = mod("driver")
    status = mongo_status(TABLE[qid].database, read_only=read_only)
    seen: list[Any] = []
    conns: list[FakeMongoConnection] = []

    def connector(config: Any, timeout: float) -> Any:
        seen.append(config)
        conns.append(FakeMongoConnection(status))
        return conns[-1]

    patch_connectors(monkeypatch, mongo=connector)
    env = product_env(qid, FAKE_DB_PW)
    note = driver.Backends().db_precheck(_spec(qid), env)
    if read_only:
        assert note is None
    else:
        assert note == account_problem(status)
        assert note.startswith("MONGODB_ACCOUNT_NOT_READ_ONLY")
    assert seen and conns
    assert any("connectionStatus" in spec for _, spec in conns[0].commands)
    config = seen[0]
    assert config.username == RO_USER and config.password == FAKE_DB_PW
    assert config.port == TABLE[qid].local_port
