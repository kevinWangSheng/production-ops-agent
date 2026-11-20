"""#666 PR-C implementer unit checks (C5 Loki / VictoriaLogs questions).

Fixed clocks only (never today's date); synthetic question directories and
the in-process stand-in Loki / VictoriaLogs (``fake_logs``); no real upstream
``expected_output`` is read, no model, PostgreSQL, web, worker, docker or
kubectl is started."""

from __future__ import annotations

import base64
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts.holmes_compare import runner
from scripts.holmes_evals import (
    driver,
    fake_logs,
    intake,
    log_backends,
    questions,
    shift,
    text,
)
from scripts.holmes_evals.setup import ScriptResult, log_setup_env
from scripts.itbench_lite.services import Started
from tests.holmes_evals_support import upstream_root

Q101 = "101_loki_historical_logs_pod_deleted"
Q102 = "102_loki_label_discovery"
Q258 = "258_victorialogs_streams"
PROMPT_101 = (
    "The payment-api pod in namespace app-101 had issues on August 2, 2025 "
    "around 13:45 UTC. What happened?"
)
ANCHOR = datetime(2025, 8, 2, 13, 45, tzinfo=UTC)
NOW = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)
SETUP = NOW - timedelta(minutes=5)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _http(
    url: str, method: str = "GET", data: bytes | None = None, auth: str | None = None
) -> tuple[int, bytes]:
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


# -- question list -------------------------------------------------------------------------


def test_question_lists_and_resolution() -> None:
    assert len(questions.C5_LOKI_QUESTIONS) == 7
    assert len(questions.C5_VICTORIALOGS_QUESTIONS) == 2
    assert questions.ALL_QUESTIONS == questions.B1_QUESTIONS + questions.C5_QUESTIONS
    assert set(log_backends.LOG_QUESTIONS) == set(questions.C5_QUESTIONS)
    assert driver.resolve_questions("c5") == questions.C5_QUESTIONS
    assert driver.resolve_questions(None) == questions.B1_QUESTIONS
    assert driver.resolve_questions("102a,257") == (
        "102a_loki_logs_transparency",
        "257_victorialogs_log_search",
    )
    with pytest.raises(driver.RoundAbort) as err:
        driver.resolve_questions("259")
    assert err.value.code == "QUESTION_AMBIGUOUS"
    assert driver.resolve_questions("259_loki_historical_logs_pod_deleted_docker")
    assert log_backends.backend_of("254_elasticsearch_dr_test_log_check") == (
        "elasticsearch"
    )
    assert shift.FIXED_TIME <= set(questions.B1_QUESTIONS)
    assert shift.LOKI_FIXED_TIME <= set(questions.C5_LOKI_QUESTIONS)


def test_spec_table_matches_pinned_checkout() -> None:
    root = upstream_root()
    if root is None:
        pytest.skip("pinned upstream checkout not present")
    for qid, spec in log_backends.LOG_QUESTIONS.items():
        q = questions.load_question(root, qid)
        for forward in q.port_forwards or []:
            assert spec.address == f"127.0.0.1:{forward['local_port']}"
            assert spec.namespace == forward["namespace"]
        if qid in shift.LOKI_FIXED_TIME:
            assert isinstance(q.user_prompt, str)
            assert (
                min(shift.question_instants(q.user_prompt)) == shift.LOKI_ANCHORS[qid]
            )
        assert ("kubectl" in (q.before_test or "")) == spec.needs_k8s


# -- English dates, whole-day plan ----------------------------------------------------------


def test_month_name_rewrite_and_deshift_invert() -> None:
    seconds = 473 * 86400
    delivered, rewrites = shift.rewrite_question(PROMPT_101, seconds)
    assert "November 18, 2026 around 13:45 UTC" in delivered
    assert [r.instant for r in rewrites] == [ANCHOR + timedelta(seconds=seconds)]
    assert shift.time_only_phrases(PROMPT_101) == 0
    assert shift.question_instants(PROMPT_101) == [ANCHOR]
    shifted = text.Shift(
        seconds, datetime(2026, 11, 18, tzinfo=UTC), datetime(2026, 11, 19, tzinfo=UTC)
    )
    report = (
        "Errors began Nov 18, 2026 at 13:47 UTC (2026-11-18T13:47:00Z); "
        "the pod was fine on November 1, 2026. It may 3, 2026."
    )
    back, count = text.deshift(report, shifted)
    assert back.startswith(
        "Errors began Aug 2, 2025 at 13:47 UTC (2025-08-02T13:47:00Z)"
    )
    assert "November 1, 2026" in back and "may 3, 2026" in back and count == 2


@pytest.mark.parametrize(
    ("hour", "fits"),
    [(0, True), (12, True), (13.5, True), (14, False), (14.7, False), (15, True)],
)
def test_whole_day_plan(hour: float, fits: bool) -> None:
    now = datetime(2026, 11, 19, tzinfo=UTC) + timedelta(hours=hour)
    if not fits:
        with pytest.raises(shift.ShiftError) as err:
            shift.plan_day_shift_seconds(ANCHOR, now)
        assert err.value.code == "SHIFT_DAY_UNAVAILABLE"
        return
    seconds = shift.plan_day_shift_seconds(ANCHOR, now)
    assert seconds % 86400 == 0
    incident = ANCHOR + timedelta(seconds=seconds)
    assert incident + shift.INCIDENT_SPAN <= now - shift.LEAD
    assert incident >= now + timedelta(seconds=130) - timedelta(hours=24)


def test_shift_day_wait() -> None:
    assert shift.seconds_to_wait_for_shift_day(Q102, NOW, 600) == 0
    assert shift.seconds_to_wait_for_shift_day(Q101, NOW, 360) == 0
    late = datetime(2026, 11, 19, 13, 30, tzinfo=UTC)  # 23h45m after 13:45
    wait = shift.seconds_to_wait_for_shift_day(Q101, late, 360)
    ready = late + timedelta(seconds=wait)
    assert ready == datetime(2026, 11, 19, 14, 46, 1, tzinfo=UTC)
    assert shift.seconds_to_wait_for_shift_day(Q101, ready, 360) == 0
    assert wait < shift.SHIFT_DAY_WAIT_CAP.total_seconds()


# -- stand-in backends and the Loki shift ----------------------------------------------------


@pytest.fixture
def loki() -> Iterator[tuple[fake_logs.FakeLoki, str, str]]:
    store, servers = fake_logs.start("loki", 0, 0, ("ro", "pw"))
    ports = [s.server_address[1] for s in servers]
    try:
        assert isinstance(store, fake_logs.FakeLoki)
        yield store, f"http://127.0.0.1:{ports[0]}", f"http://127.0.0.1:{ports[1]}"
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()


def _history() -> list[list[str]]:
    rows, t = [], datetime(2025, 8, 1, 12, tzinfo=UTC)
    while t < datetime(2025, 8, 4, 14, tzinfo=UTC):
        stamp = t.isoformat().replace("+00:00", "Z")
        rows.append([str(shift._ns(t)), json.dumps({"timestamp": stamp})])
        t += timedelta(minutes=3)
    return rows


def _push(base: str, labels: dict[str, str], values: list[list[str]]) -> None:
    body = json.dumps({"streams": [{"stream": labels, "values": values}]}).encode()
    assert _http(base + "/loki/api/v1/push", "POST", body)[0] == 204


def test_fake_gateway_is_read_only(loki: tuple[Any, str, str]) -> None:
    _, backend, gateway = loki
    _push(backend, {"namespace": "app-1"}, [[str(shift._ns(NOW)), "x"]])
    query = "/loki/api/v1/query_range?" + urllib.parse.urlencode(
        {"query": '{namespace="app-1"}'}
    )
    assert _http(gateway + query)[0] == 401
    assert _http(gateway + query, auth="ro:bad")[0] == 401
    assert _http(gateway + "/loki/api/v1/push", "POST", b"{}", auth="ro:pw")[0] == 403
    assert _http(gateway + "/loki/api/v1/labels", auth="ro:pw")[0] == 403
    assert _http(gateway + query, auth="ro:pw")[0] == 200


def test_loki_shift_pages_drops_future_and_keeps_live(
    loki: tuple[Any, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, backend, _ = loki
    monkeypatch.setattr(shift, "LOKI_PAGE", 200)
    labels = {"namespace": "app-101", "level": "INFO"}
    history = _history()
    _push(backend, labels, history)
    _push(backend, labels, [[str(shift._ns(SETUP + timedelta(seconds=30))), "live"]])
    delivered, record = shift.apply_loki_shift(
        shift.LokiAdmin(backend), Q101, PROMPT_101, NOW, setup_started=SETUP
    )
    assert "November 19, 2026 around 13:45 UTC" in delivered
    assert record.seconds % 86400 == 0 and record.backend == "loki"
    assert record.fixed_entries == len(history)
    assert record.pushed + record.dropped_future == len(history)
    assert record.dropped_future > 0 and record.line_stamps_rewritten == record.pushed
    assert record.original_max is not None
    assert record.original_max + timedelta(seconds=record.seconds) <= NOW - shift.LEAD
    rows = store.streams[tuple(sorted(labels.items()))]
    assert len(rows) == len(history) + 1 + record.pushed  # originals kept
    out = record.as_json()
    assert {"backend", "pushed", "streams"} <= set(out) and "indices" not in out
    es_record = shift.ShiftRecord(applied=False, reason="RELATIVE_TIME").as_json()
    assert "backend" not in es_record and "indices" in es_record
    # Relative Loki question: untouched.
    same, rel = shift.apply_loki_shift(
        shift.LokiAdmin(backend), Q102, "Fetch me logs", NOW, setup_started=SETUP
    )
    assert same == "Fetch me logs" and not rel.applied


def test_loki_shift_needs_fixed_entries(loki: tuple[Any, str, str]) -> None:
    _, backend, _ = loki
    with pytest.raises(shift.ShiftError) as err:
        shift.apply_loki_shift(
            shift.LokiAdmin(backend), Q101, PROMPT_101, NOW, setup_started=SETUP
        )
    assert err.value.code == "SHIFT_NO_FIXED_ENTRIES"
    with pytest.raises(shift.ShiftError) as err:
        shift.apply_loki_shift(
            shift.LokiAdmin(backend), Q101, "no time here", NOW, setup_started=SETUP
        )
    assert err.value.code == "SHIFT_NO_QUESTION_TIME"


def test_fake_victorialogs_insert_and_select() -> None:
    store, servers = fake_logs.start("victorialogs", 0, 0, ("ro", "pw"))
    backend, gateway = (f"http://127.0.0.1:{s.server_address[1]}" for s in servers)
    try:
        stamp = NOW.isoformat().replace("+00:00", "Z")
        lines = "\n".join(
            json.dumps(
                {"time": stamp, "service": s, "namespace": "app-258", "message": "m"}
            )
            for s in ("checkout-258", "inventory-258")
        )
        insert = "/insert/jsonline?_stream_fields=service,namespace&_msg_field=message&_time_field=time"
        assert _http(backend + insert, "POST", lines.encode())[0] == 200
        assert _http(gateway + insert, "POST", lines.encode(), auth="ro:pw")[0] == 403
        q = urllib.parse.urlencode(
            {"query": '{namespace="app-258"}', "start": "2026-11-19T19:00:00Z",
             "end": "2026-11-19T21:00:00Z"}
        )  # fmt: skip
        status, body = _http(gateway + "/select/logsql/streams?" + q, auth="ro:pw")
        assert status == 200
        assert len(json.loads(body)["values"]) == 2
        status, body = _http(gateway + "/select/logsql/query?" + q, auth="ro:pw")
        assert status == 200 and len(body.decode().splitlines()) == 2
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()


# -- environments ------------------------------------------------------------------------


class _Pg:
    owner_dsn = "postgresql://owner@127.0.0.1:1/x"
    runtime_dsn = "postgresql://worker@127.0.0.1:1/x"
    web_dsn = "postgresql://web@127.0.0.1:1/x"


def _cfg(tmp_path: Path, **ports: int) -> driver.Config:
    cfg = driver.Config(
        round="c1", upstream=tmp_path / "up", tmp_root=tmp_path / "tmp",
        evidence_root=tmp_path / "ev", ports=driver.Ports(**ports),
    )  # fmt: skip
    cfg.secrets = {"DEEPSEEK_API_KEY": "model-key-x", "ZAI_API_KEY": "judge-key-x"}
    cfg.k8s = driver.K8sTarget("https://127.0.0.1:29243", str(tmp_path / "ca"), "tok")
    cfg.account = intake.new_account()
    return cfg


def test_log_envs_and_isolation(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert cfg.account is not None and cfg.k8s is not None
    log_env = {
        "OPSPILOT_LOKI_URL": "http://127.0.0.1:29231",
        "OPSPILOT_LOKI_USERNAME": "ro",
        "OPSPILOT_LOKI_PASSWORD": "pw-secret",
    }
    web = driver.web_env(cfg, _Pg(), tmp_path / "ids", cfg.account, backend="loki")
    worker = driver.worker_env(
        cfg, _Pg(), {}, cfg.k8s, None, cfg.secrets, backend="loki", log_env=log_env
    )
    assert (
        web["OPSPILOT_LOKI_URL"]
        == worker["OPSPILOT_LOKI_URL"]
        == ("http://127.0.0.1:29231")
    )
    assert "OPSPILOT_ES_URL" not in web and "OPSPILOT_ES_URL" not in worker
    assert "OPSPILOT_LOKI_PASSWORD" not in web
    assert worker["OPSPILOT_LOKI_PASSWORD"] == "pw-secret"
    addresses = ["127.0.0.1:3101"]
    driver.check_env_isolation(web, "", role="web", backend_addresses=addresses)
    driver.check_env_isolation(worker, "", role="worker", backend_addresses=addresses)
    for bad, role in (
        (web | {"OPSPILOT_LOKI_PASSWORD": "x"}, "web"),
        (worker | {"VICTORIALOGS_URL": "http://127.0.0.1:29428"}, "worker"),
        (worker | {"OPSPILOT_LOKI_URL": "http://127.0.0.1:3101"}, "worker"),
    ):
        with pytest.raises(driver.RoundAbort) as err:
            driver.check_env_isolation(
                bad,
                "",
                role=role,
                backend_addresses=addresses,  # type: ignore[arg-type]
            )
        assert err.value.code == "ENV_ISOLATION"
    # B1 defaults unchanged.
    assert "OPSPILOT_ES_URL" in driver.tool_env(cfg)
    assert driver.Ports().per_attempt() == {
        "pg": 29210, "web": 29280, "prometheus": 29290, "jaeger_query": 29286,
        "k8s": 29243,
    }  # fmt: skip
    env = log_setup_env(
        log_backends.LOG_QUESTIONS[Q258],
        victorialogs_url="http://127.0.0.1:29428",
        kubeconfig=tmp_path / "kc",
        environ={"PATH": "/bin", "DEEPSEEK_API_KEY": "x", "TZ": "Asia/Tokyo"},
    )
    assert env == {
        "PATH": "/bin",
        "TZ": "UTC",
        "VICTORIALOGS_URL": "http://127.0.0.1:29428",
        "KUBECONFIG": str(tmp_path / "kc"),
    }


# -- one C5 trial through the stand-ins -------------------------------------------------


def _question(root: Path, qid: str, prompt: str) -> None:
    d = root / questions.QUESTIONS_DIR / qid
    d.mkdir(parents=True)
    (d / "test_case.yaml").write_text(
        json.dumps(
            {
                "user_prompt": prompt,
                "before_test": "echo load",
                "after_test": "echo clean",
                "setup_timeout": 120,
                "expected_output": ["SYNTHETIC-ANSWER-ELEMENT"],
            }
        )
    )


def _report(summary: str) -> dict[str, Any]:
    return {
        "schema_version": "m0-report-v2",
        "assessment_status": "completed",
        "conclusion": "supported",
        "summary": summary,
        "claims": [
            {"kind": "fact", "text": "errors", "evidence_ids": ["ev-1"],
             "target_refs": ["t-1"], "time_scope_ref": "w-1"}
        ],
        "gaps": [],
        "next_steps": [],
    }  # fmt: skip


def _backends(
    cfg: driver.Config, seen: dict[str, Any], load: Any, report: dict[str, Any],
    started_at: datetime,
) -> driver.Backends:  # fmt: skip
    servers: list[Any] = []

    def start_fake(backend: str, bport: int, gport: int, work: Path) -> Any:
        store, made = fake_logs.start(backend, bport, gport, ("ro", "pw-secret"))
        servers.extend(made)
        seen["store"] = store
        prefix = "OPSPILOT_LOKI" if backend == "loki" else "OPSPILOT_VICTORIALOGS"

        def stop() -> None:
            for s in made:
                s.shutdown()
                s.server_close()

        env = {
            f"{prefix}_URL": f"http://127.0.0.1:{gport}",
            f"{prefix}_USERNAME": "ro",
            f"{prefix}_PASSWORD": "pw-secret",
        }
        return Started(f"fake-{backend}", stop), env

    def run_script(script: str | None, **kw: Any) -> ScriptResult:
        seen.setdefault("setup_env", []).append(dict(kw["env"]))
        if kw["operation"] == "before_test":
            load(kw["env"])
        stamp = started_at.isoformat()
        return ScriptResult(kw["operation"], 0, False, stamp, stamp, 0.1, "0" * 64)

    def submit(web_url: str, account: Any, **kw: Any) -> intake.SubmitResult:
        seen["question"] = kw["question"]
        return intake.SubmitResult(
            201, "inc-1", "00000000-0000-0000-0000-000000000001", False, None
        )

    def judge(job: Any) -> int:
        seen["judge_text"] = Path(job["text_file"]).read_text()
        Path(job["out"]).write_text(json.dumps({"choice": "A", "passed": True,
            "error_code": None, "attempts": 1, "prompt_sha256": "p", "request_bytes": 9}))  # fmt: skip
        return 0

    def start(name: str) -> Any:
        return lambda *a, **k: (seen.setdefault("env_" + name, a[0]), Started(name, lambda: None))[1]  # fmt: skip

    return driver.Backends(
        start_pg=lambda d, p: _Pg(),
        stop_pg=lambda pg: None,
        start_web=start("web"),
        start_worker=start("worker"),
        run_script=run_script,
        submit=submit,
        run_state=lambda dsn, run: "completed",
        collect=lambda dsn, run: (
            {"report": report, "outcome": {"report_available": True}},
            {"r": 1},
        ),
        read_tool_calls=lambda dsn, run: [],
        trace_roots=lambda p, r, e: {
            "project": None,
            "read_back": "not_exported",
            "roots": [],
        },
        judge=judge,
        port_in_use=lambda port: False,
        start_fake_log_backend=start_fake,
        namespace_gone=lambda ns, env: True,
        now=lambda: NOW,
        sleep=lambda s: None,
    )


def test_loki_trial_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSPILOT_LEDGER_DIR", str(tmp_path / "ledger"))
    cfg = _cfg(tmp_path, loki_fake=_free_port(), loki_gateway=_free_port())
    cfg.questions = (Q101,)
    _question(cfg.upstream, Q101, PROMPT_101)
    seen: dict[str, Any] = {}

    def load(env: dict[str, str]) -> None:
        base = f"http://127.0.0.1:{cfg.ports.loki_fake}"
        _push(base, {"namespace": "app-101", "level": "ERROR"}, _history())

    report = _report("Errors started November 19, 2026 around 13:50 UTC, 13:52 UTC.")
    b = _backends(cfg, seen, load, report, SETUP)
    out = driver.run_question(cfg, b, Q101, [1])
    assert out["trials"][1]["judge"]["choice"] == "A", out
    block = json.loads(cfg.summary_path(Q101, 1).read_text())["holmes_eval"]
    assert (
        block["data_plane"]["backend"] == "loki"
        and block["data_plane"]["mode"] == "fake"
    )
    assert block["shift"]["applied"] and block["shift"]["seconds"] % 86400 == 0
    assert block["shift_day_wait_seconds"] == 0
    assert {"SHIFT_WHOLE_DAYS", "LOKI_ORIGINALS_KEPT"} <= set(block["disclosures"])
    assert "DESHIFT_TIME_ONLY_UNCHANGED" not in block["disclosures"]
    assert block["setup"]["released"]["after"]["ok"]
    assert "November 19, 2026 around 13:45 UTC" in seen["question"]
    # The judge reads the original date; the time of day needs no deshift.
    assert "August 2, 2025 around 13:50 UTC, 13:52 UTC" in seen["judge_text"]
    worker = seen["env_worker"]
    assert worker["OPSPILOT_LOKI_URL"] == f"http://127.0.0.1:{cfg.ports.loki_gateway}"
    assert (
        "OPSPILOT_ES_URL" not in worker
        and "OPSPILOT_LOKI_PASSWORD" not in seen["env_web"]
    )
    assert all(
        e["TZ"] == "UTC" and "ELASTICSEARCH_API_KEY" not in e for e in seen["setup_env"]
    )
    raw = cfg.summary_path(Q101, 1).read_text()
    assert "pw-secret" not in raw and "SYNTHETIC-ANSWER-ELEMENT" not in raw


def test_258_data_window_voids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSPILOT_LEDGER_DIR", str(tmp_path / "ledger"))
    cfg = _cfg(tmp_path, victorialogs=_free_port(), victorialogs_gateway=_free_port())
    cfg.questions = (Q258,)
    _question(cfg.upstream, Q258, "Which services have logs in the last 5 minutes?")
    seen: dict[str, Any] = {}

    def load(env: dict[str, str]) -> None:
        seen["vl_url"] = env["VICTORIALOGS_URL"]

    b = _backends(cfg, seen, load, _report("ok"), NOW - timedelta(seconds=241))
    out = driver.run_question(cfg, b, Q258, [1])
    assert out["trials"][1]["status"] == "missing"
    summary = json.loads(cfg.summary_path(Q258, 1).read_text())
    assert [h["code"] for h in summary["holmes_eval"]["history"]] == [
        "DATA_WINDOW_EXCEEDED"
    ] * 3
    assert seen["vl_url"] == f"http://127.0.0.1:{cfg.ports.victorialogs}"
    # Within the window the trial completes.
    b2 = _backends(
        cfg, {}, lambda env: None, _report("ok"), NOW - timedelta(seconds=30)
    )
    driver._set_aside(cfg, Q258, 2)
    done = driver.run_question(cfg, b2, Q258, [2])
    assert done["trials"][2]["status"] == "completed"
    block = json.loads(cfg.summary_path(Q258, 2).read_text())["holmes_eval"]
    assert block["delivery"]["data_written_to_delivery_seconds"] == 30
    assert "K8S_TOOLS_UPSTREAM_DISABLED" in block["disclosures"]


# -- Holmes side ---------------------------------------------------------------------------


def test_runner_accepts_c5_and_sets_backend_env(tmp_path: Path) -> None:
    cfg = runner.Config(
        round="c1", upstream=tmp_path, holmes_python=tmp_path / "py",
        judge_path="opspilot", questions=(Q101, Q258),
        secrets={"DEEPSEEK_API_KEY": "m", "ZAI_API_KEY": "j"},
    )  # fmt: skip
    env101 = runner.pytest_env(cfg, Q101, tmp_path)
    assert env101["OHC_SHIFT_BACKEND"] == "loki"
    assert env101["OHC_LOKI_URL"] == "http://127.0.0.1:3101"
    assert "ELASTICSEARCH_URL" not in env101 and env101["TZ"] == "UTC"
    env258 = runner.pytest_env(cfg, Q258, tmp_path)
    assert env258["VICTORIALOGS_URL"] == "http://localhost:29428"
    assert env258["OHC_SHIFT_BACKEND"] == "none"
    with pytest.raises(runner.RoundAbort) as err:
        runner.preflight(cfg)
    # Past the question check and the ES requirement (no ES question chosen).
    assert err.value.code.startswith("UPSTREAM_")
