"""#666 PR-F contract: one C6 trial end to end with replaced Backends.

Independent contract tests written from the task record's "PR-F 接口"
section (pass flow, the two new ``Backends`` fields, new codes, summary
fields, ``--dry-run``) without reading the PR-F implementation. PG, web,
worker, judge are the PR-B harness's recorders
(``tests/test_holmes_evals_trial_contract.py``); setup scripts,
port-forwards, namespace checks, the account provisioning and the
pre-check are recorders. The upstream is synthetic (answer isolation);
every clock is fixed.

Covered: the order of one pass (ports free incl. the question port, cleanup
and release check, ``before_test``, port-forward, provisioning, pre-check,
PG/web/worker, delivery, collect, port-forward stopped, ``after_test`` and
release check); setup/provision/pre-check/web/worker environments; the
delivered question (verbatim); the summary's ``data_plane``, ``db_account``,
``setup.released``, disclosures and ``NO_TIME_REFERENCE``; the password
never in argv-like records, the summary, evidence, files or output; a new
password per pass; ``DB_ACCOUNT_SETUP_FAILED`` and ``DB_LOGIN_FAILED``
retried within the three chances; ``DB_ACCOUNT_NOT_READ_ONLY`` not retried;
``PORT_BUSY``; ``LOG_BACKEND_NOT_RELEASED``; ``DB_PORT_CONFLICT``;
``DB_NOT_LAB``; ``DB_QUESTION_UNKNOWN``; a B1 trial untouched;
``driver round --questions c6 --dry-run``.

Assumptions beyond the interface (also marked ``# ASSUMPTION:``):

* A fake ``provision_db_account`` / ``db_precheck`` signals an
  infrastructure failure by raising ``driver.InfraFailure`` with the
  interface's code; the pre-check returns the product's note for an account
  that is not read-only.
* ``db_precheck``'s ``product_env`` is the worker's ``OPSPILOT_SQL_*`` /
  ``OPSPILOT_MONGODB_*`` mapping.
* ``DB_PORT_CONFLICT`` may be raised by ``prepare_round`` or the trial;
  ``DB_QUESTION_UNKNOWN`` by ``prepare_round``, the trial or as a missing
  trial of ``run_question``.
* ``--dry-run`` is a flag of ``round``; it may read the env file and
  upstream, but calls no ``Backends`` field that touches the host.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts.itbench_lite.services import Started
from tests.holmes_evals_b_support import no_proxy_env  # noqa: F401 - fixture
from tests.holmes_evals_f_support import (
    ADMIN_VARS,
    C6_ALL,
    PROMPTS,
    Q217,
    Q218,
    Q219,
    Q222,
    Q223,
    Q229,
    Q230,
    RO_USER,
    TABLE,
    port_forward,
    prefix,
    product_env,
    url_key,
    write_c6_question,
)
from tests.holmes_evals_support import FIXTURES, mod, write_question
from tests.test_holmes_evals_trial_contract import (
    EXPECTED_SENTINEL,
    JUDGE,
    MODEL,
    PROMPT_275,
    Q275,
    RATIONALE_SENTINEL,
    Harness,
    _seed_275,
)

pytestmark = pytest.mark.usefixtures("no_proxy_env")

START = datetime(2026, 11, 20, 10, 0, 0, tzinfo=UTC)
NOTE_BAD = (
    "SQL_ACCOUNT_NOT_READ_ONLY: the configured account is not read-only "
    "(relation_write); nothing was run."
)


class C6Harness(Harness):
    """The PR-B fake host plus the C6 data plane."""

    def __init__(self, tmp_path: Path, start: datetime) -> None:
        super().__init__(tmp_path, start)
        self.ns_present: dict[str, bool] = {}
        self.ns_checks: list[str] = []
        self.forwards: list[dict[str, Any]] = []
        self.provisions: list[dict[str, Any]] = []
        self.prechecks: list[dict[str, Any]] = []
        self.db_passwords: list[str] = []
        self.provision_fail = 0  # how many provisions raise
        self.login_fail = 0  # how many pre-checks raise DB_LOGIN_FAILED
        self.precheck_note: str | None = None
        self.busy: dict[int, int] = {}
        self.after_cleans = lambda: True
        self.question_ports = {r.local_port for r in TABLE.values()}

    def run_script(self, script, *, operation, cwd, env, timeout_s, log_path):
        qid = Path(cwd).name
        if qid == Q275:
            return super().run_script(
                script, operation=operation, cwd=cwd, env=env,
                timeout_s=timeout_s, log_path=log_path,
            )  # fmt: skip
        self.scripts.append(
            {"operation": operation, "script": script, "cwd": Path(cwd),
             "env": dict(env), "timeout_s": timeout_s}
        )  # fmt: skip
        self.events.append(f"script:{operation}")
        started = self.now()
        ns = TABLE[qid].namespace
        if operation == "before_test":
            self.ns_present[ns] = True
        elif self.after_cleans():
            self.ns_present[ns] = False
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(f"{operation} ok\n")
        ended = self.now()
        return mod("setup").ScriptResult(
            operation=operation, exit_code=0, timed_out=False, started_at=started,
            ended_at=ended, seconds=(ended - started).total_seconds(),
            log_sha256="0" * 64, skipped=not script,
        )  # fmt: skip

    def namespace_gone(self, namespace, env):
        self.events.append(f"ns_check:{namespace}")
        self.ns_checks.append(namespace)
        return not self.ns_present.get(namespace, False)

    def port_in_use(self, port: int) -> bool:
        if port in self.question_ports:
            self.events.append(f"port_check:{port}")
        left = self.busy.get(port, 0)
        if left > 0:
            self.busy[port] = left - 1
            return True
        return False

    def start_port_forward(self, forward, env, log_path):
        self.events.append("start:port_forward")
        self.forwards.append({"forward": dict(forward), "env": dict(env)})

        def stop() -> None:
            self.events.append("stop:port_forward")

        return Started(name="port_forward", _stop=stop, info={"pid": 0})

    def provision_db_account(self, spec, env, password):
        self.events.append("provision")
        self.provisions.append({"spec": spec, "env": dict(env), "password": password})
        self.db_passwords.append(password)
        if self.provision_fail:
            self.provision_fail -= 1
            raise mod("driver").InfraFailure("DB_ACCOUNT_SETUP_FAILED", "exit=1")
        return None

    def db_precheck(self, spec, product_env):
        self.events.append("precheck")
        self.prechecks.append({"spec": spec, "env": dict(product_env)})
        if self.login_fail:
            self.login_fail -= 1
            raise mod("driver").InfraFailure("DB_LOGIN_FAILED", "30s")
        return self.precheck_note

    def _never(self, name: str):
        def call(*args: Any, **kwargs: Any) -> Any:
            self.events.append(f"never:{name}")
            raise AssertionError(f"{name} called for a database question")

        return call

    def backends(self) -> Any:
        over: dict[str, Any] = {
            "namespace_gone": self.namespace_gone,
            "start_port_forward": self.start_port_forward,
            "provision_db_account": self.provision_db_account,
            "db_precheck": self.db_precheck,
            "port_in_use": self.port_in_use,
        }
        for name in ("start_gateway", "switch_gateway", "start_victorialogs",
                     "start_fake_log_backend", "loki_admin", "import_runbooks",
                     "revoke_runbooks"):  # fmt: skip
            over[name] = self._never(name)
        return dataclasses.replace(super().backends(), **over)

    def secrets(self) -> list[str]:
        return [*super().secrets(), *self.db_passwords]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    questions, driver = mod("questions"), mod("driver")
    upstream = tmp_path / "upstream"
    for qid in C6_ALL:
        write_c6_question(upstream, qid, expected=EXPECTED_SENTINEL)
    write_question(
        upstream,
        Q275,
        {"user_prompt": PROMPT_275, "before_test": "echo seed",
         "after_test": "echo keep", "include_tool_calls": True,
         "expected_output": [EXPECTED_SENTINEL]},
    )  # fmt: skip
    monkeypatch.setattr(questions, "verify_upstream", lambda root: None)
    if hasattr(driver, "verify_upstream"):
        monkeypatch.setattr(driver, "verify_upstream", lambda root: None)
    monkeypatch.setenv("OPSPILOT_LEDGER_DIR", str(tmp_path / "ledgers"))
    monkeypatch.setenv("ZAI_API_KEY", JUDGE)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    env_file = tmp_path / "secrets.env"
    env_file.write_text(f"DEEPSEEK_API_KEY={MODEL}\n")
    env_file.chmod(0o600)
    kube = tmp_path / "engineer.kubeconfig"
    kube.write_text("apiVersion: v1\nkind: Config\n")
    made: list[Any] = []

    def make(qids: tuple[str, ...], start: datetime = START, **ports: int):
        from tests.holmes_evals_c_support import free_port

        h = C6Harness(tmp_path, start)
        p = dataclasses.replace(driver.Ports(), es=free_port(), **ports)
        cfg = driver.Config(
            round="t1", upstream=upstream, questions=qids, ports=p,
            es_mode="fake", kubeconfig=kube, env_file=env_file, trace="off",
            judge=True, allow_dirty=True, tmp_root=tmp_path / "tmp",
            evidence_root=tmp_path / "evidence", poll_seconds=0.0,
        )  # fmt: skip
        round_ = driver.Round()
        made.append(round_)
        driver.prepare_round(cfg, h.backends(), round_)
        return h, cfg

    yield make
    for round_ in made:
        round_.close()


def _load(cfg: Any, qid: str) -> Any:
    return mod("questions").load_question(Path(cfg.upstream), qid)


def _summary(cfg: Any, qid: str, n: int = 1) -> tuple[dict, str]:
    path = Path(cfg.evidence_dir) / qid / f"trial-{n}" / "summary.json"
    raw = path.read_text(encoding="utf-8")
    return json.loads(raw), raw


def _all_files_text(root: Path) -> str:
    return "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in sorted(root.rglob("*"))
        if p.is_file()
    )


def _no_db_password_anywhere(h: C6Harness, tmp_path: Path, *texts: str) -> None:
    assert h.db_passwords
    blob = _all_files_text(tmp_path) + "\n".join(texts)
    for pw in h.db_passwords:
        assert pw not in blob, "database password leaked"


def _last(ev: list[str], name: str) -> int:
    return len(ev) - 1 - ev[::-1].index(name)


# -- one full pass -------------------------------------------------------------------------


def test_sql_trial_end_to_end(world, tmp_path: Path, capsys) -> None:
    driver = mod("driver")
    h, cfg = world((Q217,))
    assert "start_fake_es" not in h.events  # no B1 question: no ES
    assert "readonly_identity" not in h.events
    record = driver.run_trial(cfg, h.backends(), Q217, _load(cfg, Q217), 1)
    assert record["status"] == "completed", record
    assert not record["history"]
    ev = h.events
    first = ev.index
    assert not [e for e in ev if e.startswith("never:")]
    assert "es_admin" not in ev

    # Order of the pass.
    ops = [s["operation"] for s in h.scripts]
    assert ops == ["after_test", "before_test", "after_test"]
    assert first("port_check:10217") < first("script:after_test")
    assert first("script:after_test") < first("ns_check:app-217")
    assert first("ns_check:app-217") < first("script:before_test")
    assert first("script:before_test") < first("start:port_forward")
    assert first("start:port_forward") < first("provision") < first("precheck")
    assert first("precheck") < first("start:pg") < first("start:web")
    assert first("start:web") < first("start:worker") < first("submit")
    assert first("submit") < first("collect") < first("stop:port_forward")
    last_after = _last(ev, "script:after_test")
    assert first("stop:port_forward") < last_after
    assert last_after < _last(ev, "ns_check:app-217")
    assert last_after < _last(ev, "port_check:10217")
    assert ev.count("provision") == 1 and ev.count("precheck") == 1

    # Setup scripts and the port-forward: engineer kubeconfig only.
    for s in h.scripts:
        assert s["cwd"] == Path(cfg.upstream) / FIXTURES / Q217
        assert s["timeout_s"] == 120
        assert s["env"]["KUBECONFIG"] == str(cfg.kubeconfig)
        assert not [
            k for k in s["env"] if k.startswith(("OPSPILOT_", "ELASTICSEARCH_"))
        ]
        for name in ("DEEPSEEK_API_KEY", "ZAI_API_KEY", *ADMIN_VARS):
            assert name not in s["env"]
    (fw,) = h.forwards
    assert fw["forward"] == port_forward(Q217)
    assert fw["env"]["KUBECONFIG"] == str(cfg.kubeconfig)

    # Provisioning: the question's spec, engineer env, a fresh password.
    (prov,) = h.provisions
    assert prov["spec"].qid == Q217
    assert prov["env"].get("KUBECONFIG") == str(cfg.kubeconfig)
    assert not [k for k in prov["env"] if k.startswith(("OPSPILOT_", "ELASTICSEARCH_"))]
    password = prov["password"]
    assert isinstance(password, str) and len(password) >= 24

    # Product processes.
    (web,) = h.web_envs
    (worker,) = h.worker_envs
    expected = product_env(Q217, password)
    for env in (web, worker):
        assert not [k for k in env if k.startswith(
            ("OPSPILOT_ES_", "ELASTICSEARCH_", "OPSPILOT_LOKI_",
             "OPSPILOT_VICTORIALOGS_", "OPSPILOT_MONGODB_"))]  # fmt: skip
        assert env["OPSPILOT_SQL_URL"] == "postgresql://127.0.0.1:10217/holmesdb"
        for name in ADMIN_VARS:
            assert name not in env
    assert [k for k in web if k.startswith("OPSPILOT_SQL_")] == ["OPSPILOT_SQL_URL"]
    assert {
        k: v for k, v in worker.items() if k.startswith("OPSPILOT_SQL_")
    } == expected
    assert password not in json.dumps(web)
    (pre,) = h.prechecks
    assert pre["spec"].qid == Q217
    assert {
        k: v for k, v in pre["env"].items() if k.startswith("OPSPILOT_SQL_")
    } == expected

    # The question goes verbatim (no time reference, no shift).
    (sub,) = h.submits
    assert sub["question"] == PROMPTS[Q217]

    # Summary.
    summary, raw = _summary(cfg, Q217)
    he = summary["holmes_eval"]
    dp = he["data_plane"]
    assert dp["backend"] == "sql" and dp["dialect"] == "postgresql"
    assert dp["mode"] == "port-forward"
    assert dp["product_url"] == "postgresql://127.0.0.1:10217/holmesdb"
    assert dp["port_forwards"] == [port_forward(Q217)]
    assert dp["tls"] == "disable"
    assert dp["victorialogs_image"] is None
    acct = he["db_account"]
    assert acct["user"] == RO_USER
    assert acct["grants"] == "C6-21:postgresql"
    assert acct["precheck"] == "pass"
    assert isinstance(acct["provision_seconds"], (int, float))
    assert acct["provision_seconds"] >= 0
    released = he["setup"]["released"]
    for phase in ("before", "after"):
        assert released[phase]["namespace_gone"] is True
        assert list(released[phase]["busy_ports"]) == []
    shift = he["shift"]
    assert shift["applied"] is False and shift["reason"] == "NO_TIME_REFERENCE"
    codes = set(he["disclosures"])
    assert {
        "DB_READ_ONLY_ACCOUNT",
        "DB_TOOL_SURFACE_DIFFERS",
        "K8S_SCOPE_DIFFERS",
    } <= codes
    assert "MSSQL_NO_TLS" not in codes
    assert "SKILL_ASSERTION_DROPPED" not in codes
    assert not codes & {"DESHIFT_TIME_ONLY_UNCHANGED", "DESHIFT_DATE_MIDPOINT",
                        "SHIFT_WHOLE_DAYS", "LOKI_ORIGINALS_KEPT"}  # fmt: skip
    assert he["judge"]["choice"] == "A" and he["category"] == "ok"
    for forbidden in (EXPECTED_SENTINEL, RATIONALE_SENTINEL):
        assert forbidden not in raw
    for secret in h.secrets():
        assert secret not in raw, secret[:6]
    out = capsys.readouterr()
    _no_db_password_anywhere(h, tmp_path, out.out, out.err)
    assert driver.trial_state(cfg, Q217, 1) == "done"


@pytest.mark.parametrize("qid", [Q218, Q219, Q222, Q223, Q229, Q230])
def test_each_db_question_pass(world, tmp_path: Path, qid: str) -> None:
    driver = mod("driver")
    h, cfg = world((qid,))
    record = driver.run_trial(cfg, h.backends(), qid, _load(cfg, qid), 1)
    assert record["status"] == "completed", record
    row = TABLE[qid]
    (prov,) = h.provisions
    password = prov["password"]
    (web,) = h.web_envs
    (worker,) = h.worker_envs
    expected = product_env(qid, password)
    assert [k for k in web if k.startswith(prefix(qid))] == [url_key(qid)]
    assert {k: v for k, v in worker.items() if k.startswith(prefix(qid))} == expected
    other = "OPSPILOT_SQL_" if row.backend == "mongodb" else "OPSPILOT_MONGODB_"
    assert not [k for k in {**web, **worker} if k.startswith(other)]
    for s in h.scripts:
        assert s["timeout_s"] == row.setup_timeout_s
    (fw,) = h.forwards
    assert fw["forward"] == port_forward(qid)
    he = _summary(cfg, qid)[0]["holmes_eval"]
    dp = he["data_plane"]
    assert dp["backend"] == row.backend and dp["dialect"] == row.dialect
    assert dp["mode"] == "port-forward"
    assert dp["product_url"] == expected[url_key(qid)]
    assert dp["tls"] == row.tls
    assert he["db_account"]["grants"] == f"C6-21:{row.dialect}"
    assert he["db_account"]["user"] == RO_USER
    codes = set(he["disclosures"])
    assert {"DB_READ_ONLY_ACCOUNT", "DB_TOOL_SURFACE_DIFFERS"} <= codes
    assert ("MSSQL_NO_TLS" in codes) is (qid == Q223)
    assert he["shift"]["reason"] == "NO_TIME_REFERENCE"
    assert h.submits[0]["question"] == PROMPTS[qid]
    _no_db_password_anywhere(h, tmp_path)


def test_new_password_every_pass(world, tmp_path: Path) -> None:
    driver = mod("driver")
    h, cfg = world((Q229,))
    for trial in (1, 2):
        record = driver.run_trial(cfg, h.backends(), Q229, _load(cfg, Q229), trial)
        assert record["status"] == "completed"
    first, second = h.db_passwords
    assert first != second
    assert h.worker_envs[0]["OPSPILOT_MONGODB_PASSWORD"] == first
    assert h.worker_envs[1]["OPSPILOT_MONGODB_PASSWORD"] == second
    _no_db_password_anywhere(h, tmp_path)


# -- failures and retries --------------------------------------------------------------------


def test_provision_failure_retried_then_passes(world) -> None:
    driver = mod("driver")
    h, cfg = world((Q217,))
    h.provision_fail = 1
    record = driver.run_trial(cfg, h.backends(), Q217, _load(cfg, Q217), 1)
    assert record["status"] == "completed"
    (first,) = record["history"]
    assert first["code"] == "DB_ACCOUNT_SETUP_FAILED" and first["kind"] == "infra"
    assert h.events.count("provision") == 2
    assert len(h.submits) == 1
    # The failed chance's port-forward was stopped before the next one.
    assert h.events.count("start:port_forward") == h.events.count("stop:port_forward")


def test_provision_failure_three_times_is_missing(world, tmp_path: Path) -> None:
    driver = mod("driver")
    h, cfg = world((Q217,))
    h.provision_fail = 3
    record = driver.run_trial(cfg, h.backends(), Q217, _load(cfg, Q217), 1)
    assert record["status"] == "missing"
    summary, raw = _summary(cfg, Q217)
    history = summary["holmes_eval"]["history"]
    assert [e["code"] for e in history] == ["DB_ACCOUNT_SETUP_FAILED"] * 3
    assert {e["kind"] for e in history} == {"infra"}
    assert h.submits == []
    assert "precheck" not in h.events and "start:worker" not in h.events
    _no_db_password_anywhere(h, tmp_path, raw)


def test_login_failure_is_infra_and_retried(world) -> None:
    driver = mod("driver")
    h, cfg = world((Q222,))
    h.login_fail = 1
    record = driver.run_trial(cfg, h.backends(), Q222, _load(cfg, Q222), 1)
    assert record["status"] == "completed"
    (first,) = record["history"]
    assert first["code"] == "DB_LOGIN_FAILED" and first["kind"] == "infra"
    assert h.events.count("precheck") == 2
    assert h.events.count("provision") == 2  # a new pass provisions again


def test_not_read_only_account_is_deterministic_and_not_retried(
    world, tmp_path: Path
) -> None:
    driver = mod("driver")
    h, cfg = world((Q217,))
    h.precheck_note = NOTE_BAD
    with pytest.raises(driver.DeterministicError) as caught:
        driver.run_trial(cfg, h.backends(), Q217, _load(cfg, Q217), 1)
    assert caught.value.code == "DB_ACCOUNT_NOT_READ_ONLY"
    assert NOTE_BAD in caught.value.detail
    assert h.events.count("provision") == 1 and h.events.count("precheck") == 1
    for name in ("start:pg", "start:web", "start:worker", "submit"):
        assert name not in h.events
    for pw in h.db_passwords:
        assert pw not in str(caught.value) and pw not in caught.value.detail


def test_not_read_only_account_trial_recorded_missing(world, tmp_path: Path) -> None:
    driver = mod("driver")
    h, cfg = world((Q229,))
    h.precheck_note = (
        "MONGODB_ACCOUNT_NOT_READ_ONLY: the configured account is not read-only "
        "(role readWrite@shopdb); nothing was run."
    )
    result = driver.run_question(cfg, h.backends(), Q229, [1])
    assert result["trials"][1]["status"] == "missing"
    summary, raw = _summary(cfg, Q229)
    assert summary["status"] == "missing"
    assert "DB_ACCOUNT_NOT_READ_ONLY" in raw
    assert h.events.count("provision") == 1
    _no_db_password_anywhere(h, tmp_path, raw)


def test_busy_question_port_is_port_busy_and_retried(world) -> None:
    driver = mod("driver")
    h, cfg = world((Q229,))
    h.busy[10227] = 1
    record = driver.run_trial(cfg, h.backends(), Q229, _load(cfg, Q229), 1)
    assert record["status"] == "completed"
    (first,) = record["history"]
    assert first["code"] == "PORT_BUSY" and first["kind"] == "infra"
    assert "question:10227" in first["detail"]


def test_leftover_namespace_before_the_pass_is_retried(world) -> None:
    driver = mod("driver")
    h, cfg = world((Q223,))
    h.ns_present["app-223"] = True
    calls = {"n": 0}

    def cleans() -> bool:
        calls["n"] += 1
        return calls["n"] >= 2

    h.after_cleans = cleans
    record = driver.run_trial(cfg, h.backends(), Q223, _load(cfg, Q223), 1)
    assert record["status"] == "completed"
    first, *_ = record["history"]
    assert first["code"] == "LOG_BACKEND_NOT_RELEASED" and first["kind"] == "infra"
    assert len(h.submits) == 1


def test_namespace_left_after_the_pass_aborts_cleanup(world) -> None:
    driver = mod("driver")
    h, cfg = world((Q217,))
    state = {"before": 0}
    real = h.run_script

    def run_script(script, **kw):
        if kw["operation"] == "before_test":
            state["before"] += 1
        return real(script, **kw)

    h.run_script = run_script  # type: ignore[method-assign]
    h.after_cleans = lambda: state["before"] == 0
    with pytest.raises(driver.RoundAbort) as caught:
        driver.run_trial(cfg, h.backends(), Q217, _load(cfg, Q217), 1)
    assert caught.value.code == "CLEANUP_FAILED"


def test_question_port_equal_to_product_pg_is_db_port_conflict(world) -> None:
    driver = mod("driver")
    with pytest.raises(driver.RoundAbort) as caught:
        h, cfg = world((Q217,), pg=10217)
        driver.run_trial(cfg, h.backends(), Q217, _load(cfg, Q217), 1)
    assert caught.value.code == "DB_PORT_CONFLICT"


def test_worker_env_not_lab_is_db_not_lab(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    import opspilot.tracing as tracing

    driver = mod("driver")
    original = tracing.check_lab_target

    def not_lab(env):
        return (*original(env), "SQL_NOT_LAB")

    monkeypatch.setattr(tracing, "check_lab_target", not_lab)
    for name, module in list(sys.modules.items()):
        if name.startswith("scripts.holmes_evals.") and module is not None:
            if getattr(module, "check_lab_target", None) is original:
                monkeypatch.setattr(module, "check_lab_target", not_lab)
    h, cfg = world((Q217,))
    with pytest.raises(driver.RoundAbort) as caught:
        driver.run_trial(cfg, h.backends(), Q217, _load(cfg, Q217), 1)
    assert caught.value.code == "DB_NOT_LAB"
    assert "start:worker" not in h.events and h.submits == []
    for pw in h.db_passwords:
        assert pw not in str(caught.value)


def test_trace_off_is_not_db_not_lab(world) -> None:
    # check_lab_target also reports TRACE_MODE_NOT_LAB etc. with trace off;
    # only SQL_NOT_LAB / MONGODB_NOT_LAB abort (covered end to end above).
    driver = mod("driver")
    h, cfg = world((Q230,))
    assert cfg.trace == "off"
    record = driver.run_trial(cfg, h.backends(), Q230, _load(cfg, Q230), 1)
    assert record["status"] == "completed"


def test_c6_question_missing_from_table_is_db_question_unknown(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    driver, db = mod("driver"), mod("db_backends")
    table = db.DB_QUESTIONS
    smaller = {k: v for k, v in table.items() if k != Q219}
    try:
        monkeypatch.delitem(table, Q219)
    except TypeError:
        for name, module in list(sys.modules.items()):
            if name.startswith("scripts.holmes_evals.") and module is not None:
                for attr, value in list(vars(module).items()):
                    if value is table:
                        monkeypatch.setattr(module, attr, smaller)
    code = None
    try:
        h, cfg = world((Q219,))
        result = driver.run_question(cfg, h.backends(), Q219, [1])
        code = result["trials"][1].get("code")
        if code is None:
            code = json.dumps(_summary(cfg, Q219)[0])
    except driver.DeterministicError as error:
        code = error.code
    assert code is not None and "DB_QUESTION_UNKNOWN" in code


def test_b1_trial_untouched(world) -> None:
    driver = mod("driver")
    h, cfg = world((Q275,), start=datetime(2026, 11, 18, 10, 0, tzinfo=UTC))
    _seed_275(h)
    record = driver.run_trial(cfg, h.backends(), Q275, _load(cfg, Q275), 1)
    assert record["status"] == "completed"
    assert "provision" not in h.events and "precheck" not in h.events
    he = _summary(cfg, Q275)[0]["holmes_eval"]
    assert "db_account" not in he
    assert he["data_plane"]["backend"] == "elasticsearch"
    codes = set(he["disclosures"])
    assert not codes & {"DB_READ_ONLY_ACCOUNT", "DB_TOOL_SURFACE_DIFFERS",
                        "MSSQL_NO_TLS"}  # fmt: skip
    for env in (*h.web_envs, *h.worker_envs):
        assert not [k for k in env if k.startswith(("OPSPILOT_SQL_",
                                                    "OPSPILOT_MONGODB_"))]  # fmt: skip


# -- dry run ---------------------------------------------------------------------------------


def test_dry_run_prints_and_executes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import secrets as secrets_mod

    questions, driver = mod("questions"), mod("driver")
    upstream = tmp_path / "upstream"
    for qid in C6_ALL:
        write_c6_question(upstream, qid, expected=EXPECTED_SENTINEL)
    monkeypatch.setattr(questions, "verify_upstream", lambda root: None)
    if hasattr(driver, "verify_upstream"):
        monkeypatch.setattr(driver, "verify_upstream", lambda root: None)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("ZAI_API_KEY", JUDGE)
    sentinel = "dry-" + "pw-" + "fake-23"
    monkeypatch.setattr(secrets_mod, "token_urlsafe", lambda n=None: sentinel)
    env_file = tmp_path / "secrets.env"
    env_file.write_text(f"DEEPSEEK_API_KEY={MODEL}\n")
    env_file.chmod(0o600)
    kube = tmp_path / "engineer.kubeconfig"
    kube.write_text("apiVersion: v1\nkind: Config\n")
    called: list[str] = []
    harmless = {"now", "sleep", "port_in_use", "unreachable_k8s"}

    def refuse(name: str):
        def call(*args: Any, **kwargs: Any) -> Any:
            called.append(name)
            raise AssertionError(f"dry run called {name}")

        return call

    defaults = driver.Backends()
    backends = dataclasses.replace(
        defaults,
        **{
            f.name: refuse(f.name)
            for f in dataclasses.fields(driver.Backends)
            if f.name not in harmless
        },
        port_in_use=lambda port: False,
    )
    code = driver.main(
        ["round", "--round", "t1", "--upstream", str(upstream), "--questions", "c6",
         "--dry-run", "--env-file", str(env_file), "--kubeconfig", str(kube),
         "--allow-dirty", "--tmp-root", str(tmp_path / "tmp"),
         "--evidence-root", str(tmp_path / "evidence")],
        backends=backends,
    )  # fmt: skip
    out = capsys.readouterr()
    text = out.out + out.err
    assert code == 0, text
    assert called == []
    for qid in C6_ALL:
        assert qid in text
        assert product_env(qid, "x")[url_key(qid)] in text
    assert RO_USER in text
    assert sentinel not in text
    assert MODEL not in text and JUDGE not in text
    assert not list((tmp_path / "evidence").rglob("summary.json"))
