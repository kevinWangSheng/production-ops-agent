"""#666 PR-B implementer unit checks. Synthetic question directories and an
in-process stand-in Elasticsearch only; no real upstream ``expected_output``
is read, no model, PostgreSQL, web or worker is started."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts.holmes_evals import driver, fake_es, intake, questions, shift, text
from scripts.holmes_evals.setup import ScriptResult, setup_env
from scripts.holmes_evals.tool_calls import tool_calls_from_rows
from scripts.itbench_lite.services import Started

ENGINEER = "ENGINEER-KEY-SYNTHETIC-1234"
QID = "275_es_field_name_correction"
REL_QID = "254_elasticsearch_dr_test_log_check"


@pytest.fixture
def es() -> Iterator[tuple[fake_es.FakeElasticsearch, str]]:
    store = fake_es.FakeElasticsearch(ENGINEER)
    server = fake_es.serve(store, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield store, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _h(key: str = ENGINEER) -> dict[str, str]:
    return {"Authorization": f"ApiKey {key}"}


def _call(
    store: fake_es.FakeElasticsearch,
    method: str,
    path: str,
    body: Any = None,
    key: str = ENGINEER,
    **q: str,
) -> tuple[int, Any]:
    raw = (
        b""
        if body is None
        else (body.encode() if isinstance(body, str) else json.dumps(body).encode())
    )
    return store.handle(method, path, {k: [v] for k, v in q.items()}, _h(key), raw)


def _load_275(store: fake_es.FakeElasticsearch) -> None:
    _call(store, "PUT", "/app-275-logs-x", {"mappings": {"properties": {
        "severity": {"type": "keyword"}, "message": {"type": "text"}, "@timestamp": {"type": "date"}}}})  # fmt: skip
    lines = []
    for i in range(1, 4):
        lines.append(json.dumps({"index": {}}))
        lines.append(json.dumps({"severity": "ERROR", "message": f"DB connection refused {i}",
                                 "@timestamp": f"2026-05-14T12:00:0{i}Z"}))  # fmt: skip
    _call(store, "POST", "/app-275-logs-x/_bulk", "\n".join(lines) + "\n")


# -- fake ES ---------------------------------------------------------------------


def test_fake_es_auth_and_read_only_keys() -> None:
    store = fake_es.FakeElasticsearch(ENGINEER)
    assert _call(store, "GET", "/_cat/indices", key="nope")[0] == 401
    status, issued = _call(store, "POST", "/_security/api_key", {"name": "ro"})
    assert status == 200
    ro = issued["encoded"]
    assert _call(store, "PUT", "/app-1-x", {}, key=ro)[0] == 403
    assert _call(store, "PUT", "/app-1-x", {})[0] == 200
    assert _call(store, "GET", "/app-1-x/_mapping", key=ro)[0] == 200
    assert _call(store, "POST", "/_security/api_key", {}, key=ro)[0] == 403


def test_fake_es_search_semantics() -> None:
    store = fake_es.FakeElasticsearch(ENGINEER)
    _load_275(store)
    status, out = _call(
        store,
        "POST",
        "/app-275-logs-x/_search",
        {"query": {"term": {"level": "ERROR"}}, "size": 0},
    )
    assert status == 200 and out["hits"]["total"]["value"] == 0
    body = {
        "query": {"bool": {"filter": [{"range": {"@timestamp": {"gte": "2026-05-14T12:00:02Z"}}}],
                           "must": [{"term": {"severity": "ERROR"}}]}},
        "sort": [{"@timestamp": {"order": "desc"}}],
        "aggs": {"lo": {"min": {"field": "@timestamp"}}},
        "size": 1,
    }  # fmt: skip
    status, out = _call(store, "POST", "/app-275-logs-x/_search", body)
    assert status == 200
    assert out["hits"]["total"]["value"] == 2
    assert out["hits"]["hits"][0]["_source"]["@timestamp"] == "2026-05-14T12:00:03Z"
    assert out["aggregations"]["lo"]["value_as_string"].startswith(
        "2026-05-14T12:00:02"
    )
    status, out = _call(
        store, "POST", "/app-275-logs-x/_search", {"query": {"fuzzy": {"x": 1}}}
    )
    assert status == 400 and out["error"]["type"] == "parsing_exception"
    status, out = _call(
        store,
        "POST",
        "/app-275-logs-x/_search",
        {"aggs": {"m": {"terms": {"field": "message"}}}},
    )
    assert status == 400


def test_fake_es_dynamic_mapping_and_cat_text() -> None:
    store = fake_es.FakeElasticsearch(ENGINEER)
    _call(
        store,
        "POST",
        "/app-187-a/_doc",
        {"ts": "2024-01-15T12:00:00Z", "n": 3, "msg": "hi"},
    )
    _, mapping = _call(store, "GET", "/app-187-a/_mapping")
    props = mapping["app-187-a"]["mappings"]["properties"]
    assert props["ts"]["type"] == "date" and props["n"]["type"] == "long"
    assert props["msg"]["fields"]["keyword"]["type"] == "keyword"
    status, rows = _call(
        store,
        "GET",
        "/_cat/indices/app-187-*",
        h="index,store.size",
        s="store.size:desc",
    )
    assert status == 200 and isinstance(rows, str) and rows.startswith("app-187-a ")
    assert _call(store, "GET", "/app-9-missing/_mapping")[0] == 404
    assert _call(store, "GET", "/app-9-*/_mapping") == (200, {})


# -- shift --------------------------------------------------------------------------


def test_shift_value_keeps_format() -> None:
    day = 86400
    assert (
        shift.shift_value("2024-01-15T10:25:00.010Z", day) == "2024-01-16T10:25:00.010Z"
    )
    assert shift.shift_value("2024-01-14 00:00:00", 60) == "2024-01-14 00:01:00"
    assert (
        shift.shift_value("2024-01-15T10:00:00+02:00", 3600)
        == "2024-01-15T11:00:00+02:00"
    )
    assert shift.shift_value(1_700_000_000_000, 60) == 1_700_000_060_000
    assert shift.shift_value("not a date", 60) is None
    assert shift.shift_value(True, 60) is None


def test_shift_document_only_date_fields() -> None:
    doc = {
        "@timestamp": "2024-01-15T10:00:00Z",
        "message": "at 2024-01-15T10:00:00Z",
        "a": {"t": ["2024-01-15T10:00:00Z"]},
    }
    new, rewritten, unparsed = shift.shift_document(doc, ["@timestamp", "a.t"], 60)
    assert new["@timestamp"] == "2024-01-15T10:01:00Z"
    assert new["a"]["t"] == ["2024-01-15T10:01:00Z"]
    assert new["message"] == doc["message"]
    assert (rewritten, unparsed) == (2, 0)


def test_plan_shift_whole_minutes_and_overlap() -> None:
    anchor = datetime(2026, 5, 14, 12, 2, 15, tzinfo=UTC)
    now = datetime(2026, 11, 18, 19, 0, 50, tzinfo=UTC)
    seconds = shift.plan_shift_seconds(anchor, now)
    assert seconds % 60 == 0
    assert anchor + timedelta(seconds=seconds) <= now - shift.LEAD
    with pytest.raises(shift.ShiftError) as err:
        shift.plan_shift_seconds(now, now)
    assert err.value.code == "SHIFT_NOT_NEEDED"
    with pytest.raises(shift.ShiftError) as err:
        shift.check_no_overlap(anchor - timedelta(hours=3), anchor, 3600)
    assert err.value.code == "SHIFT_OVERLAP"
    shift.check_no_overlap(anchor - timedelta(hours=3), anchor, seconds)


def test_rewrite_question_inverts_with_deshift() -> None:
    seconds = 16_268_280  # whole minutes, not whole days
    q = "Between 2026-05-14T12:00:00Z and 2026-05-14 12:05 on 2026-05-14, at 14:00."
    new, rewrites = shift.rewrite_question(q, seconds)
    assert len(rewrites) == 3 and new != q
    assert shift.time_only_phrases(q) == 1
    lo = min(r.instant for r in rewrites)
    hi = max(r.instant for r in rewrites)
    back, count = text.deshift(new, text.Shift(seconds, lo, hi))
    assert count == 3 and back == q


def test_apply_shift_rewrites_data_and_records(
    es: tuple[fake_es.FakeElasticsearch, str],
) -> None:
    store, url = es
    _load_275(store)
    now = datetime(2026, 11, 18, 19, 0, 50, tzinfo=UTC)
    admin = shift.EsAdmin(url, ENGINEER)
    delivered, record = shift.apply_shift(
        admin, QID, "count ERROR in app-275-logs-x", now
    )
    assert record.applied and record.seconds % 60 == 0
    assert record.indices[0]["rewritten"] == 3
    assert record.shifted_max is not None and record.shifted_max <= now - shift.LEAD
    _, out = _call(
        store,
        "POST",
        "/app-275-logs-x/_search",
        {"sort": [{"@timestamp": "desc"}], "size": 1},
    )
    assert out["hits"]["hits"][0]["_source"]["@timestamp"].startswith("2026-11-18")
    assert delivered == "count ERROR in app-275-logs-x"
    assert record.as_text_shift() is not None
    _, relative = shift.apply_shift(admin, REL_QID, "x", now)
    assert relative.applied is False and relative.reason == "RELATIVE_TIME"
    assert relative.as_text_shift() is None


# -- environments, intake, small rules -------------------------------------------------


@dataclass
class _Pg:
    owner_dsn: str = "postgresql://owner@127.0.0.1:1/x"
    runtime_dsn: str = "postgresql://worker@127.0.0.1:1/x"
    web_dsn: str = "postgresql://web@127.0.0.1:1/x"


def _cfg(tmp_path: Path) -> driver.Config:
    cfg = driver.Config(round="t1", upstream=tmp_path / "up", tmp_root=tmp_path / "tmp",
                        evidence_root=tmp_path / "ev", es_url="http://127.0.0.1:29292")  # fmt: skip
    cfg.engineer_key = ENGINEER
    cfg.secrets = {"DEEPSEEK_API_KEY": "model-key-x", "ZAI_API_KEY": "judge-key-x"}
    cfg.product_es = {"OPSPILOT_ES_URL": cfg.es_url, "OPSPILOT_ES_API_KEY": "ro-key"}
    cfg.k8s = driver.K8sTarget(
        "https://127.0.0.1:29243", str(tmp_path / "ca.crt"), "tok"
    )
    cfg.account = intake.new_account()
    return cfg


def test_env_isolation(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert cfg.account is not None and cfg.k8s is not None
    web = driver.web_env(cfg, _Pg(), tmp_path / "ids.json", cfg.account)
    worker = driver.worker_env(
        cfg, _Pg(), cfg.product_es, cfg.k8s, "production-eu-west-2", cfg.secrets
    )
    driver.check_env_isolation(web, ENGINEER, role="web")
    driver.check_env_isolation(worker, ENGINEER, role="worker")
    assert "OPSPILOT_ES_API_KEY" not in web and "DEEPSEEK_API_KEY" not in web
    assert worker["OPSPILOT_CLUSTER_NAME"] == "production-eu-west-2"
    assert worker["OPSPILOT_WORKER_CAPACITY"] == "1"
    assert "ZAI_API_KEY" not in worker
    plain = driver.worker_env(cfg, _Pg(), cfg.product_es, cfg.k8s, None, cfg.secrets)
    assert "OPSPILOT_CLUSTER_NAME" not in plain
    setup = setup_env(
        cfg.es_url, ENGINEER, None, {"PATH": "/bin", "DEEPSEEK_API_KEY": "x"}
    )
    assert (
        setup["ELASTICSEARCH_API_KEY"] == ENGINEER and "DEEPSEEK_API_KEY" not in setup
    )
    for bad, role in ((web | {"ELASTICSEARCH_URL": "x"}, "web"), (worker | {"X": ENGINEER}, "worker"),
                      (web | {"DEEPSEEK_API_KEY": "k"}, "web")):  # fmt: skip
        with pytest.raises(driver.RoundAbort) as err:
            driver.check_env_isolation(bad, ENGINEER, role=role)  # type: ignore[arg-type]
        assert err.value.code == "ENV_ISOLATION"


def test_small_rules() -> None:
    assert intake.idempotency_key("r1", QID, 2, 3) == f"holmes-r1-{QID}-t2-c3"
    t0 = datetime(2026, 11, 18, 23, 55, tzinfo=UTC)
    assert driver.setup_to_delivery_ok(t0, t0 + timedelta(seconds=120))
    assert not driver.setup_to_delivery_ok(t0, t0 + timedelta(seconds=121))
    assert driver.seconds_to_wait_for_utc_midnight(
        "280_es_multi_quirk", t0
    ) == pytest.approx(305)
    assert driver.seconds_to_wait_for_utc_midnight(QID, t0) == 0
    assert driver.resolve_questions("254,275") == (REL_QID, QID)
    with pytest.raises(driver.RoundAbort):
        driver.resolve_questions("186")


def test_tool_calls_from_rows_order_and_refusals() -> None:
    steps = [
        {"sequence": 2, "step_id": "b", "status": "committed", "response": {}, "tool_results": [
            {"ordinal": 0, "result": {"status": "denied", "tool": "elasticsearch_search",
                                      "reason": "INVALID_JSON", "query": {"index": "a"}}}]},
        {"sequence": 1, "step_id": "a", "status": "committed", "response": {}, "tool_results": [
            {"ordinal": 1, "result": {"status": "ok", "evidence_id": "e2"}},
            {"ordinal": 0, "result": {"status": "ok", "evidence_id": "e1"}}]},
    ]  # fmt: skip
    views = {
        "e1": {
            "tool": "elasticsearch_list_indices",
            "query": {"pattern": "*"},
            "content": [1],
        },
        "e2": {
            "tool": "elasticsearch_mappings",
            "query": {"index": "a"},
            "content": [2],
        },
    }
    calls = tool_calls_from_rows(steps, views)
    assert [c.tool for c in calls] == [
        "elasticsearch_list_indices",
        "elasticsearch_mappings",
        "elasticsearch_search",
    ]
    assert calls[0].output == views["e1"] and calls[2].refused_code == "denied"
    assert calls[2].refused_reason == "INVALID_JSON"


# -- one trial through fake backends ----------------------------------------------------


def _upstream(root: Path) -> None:
    d = root / questions.QUESTIONS_DIR / QID
    d.mkdir(parents=True)
    (d / "test_case.yaml").write_text(
        "user_prompt: 'In app-275-logs-x count ERROR.'\n"
        "before_test: 'echo load'\nafter_test: 'echo clean'\n"
        "expected_output: ['SYNTHETIC-ANSWER-ELEMENT']\n"
    )


REPORT = {
    "schema_version": "m0-report-v2",
    "assessment_status": "completed",
    "conclusion": "supported",
    "summary": "3 ERROR documents at 2026-11-18T18:58:01Z.",
    "claims": [
        {
            "kind": "fact",
            "text": "3 ERROR documents",
            "evidence_ids": ["ev-1"],
            "target_refs": ["t-1"],
            "time_scope_ref": "w-1",
        }
    ],
    "gaps": [],
    "next_steps": [],
}


def _backends(
    store: fake_es.FakeElasticsearch, url: str, seen: dict[str, Any]
) -> driver.Backends:
    def run_script(script: str | None, **kw: Any) -> ScriptResult:
        if kw["operation"] == "before_test":
            _load_275(store)
        seen.setdefault("scripts", []).append(
            (kw["operation"], kw["env"]["ELASTICSEARCH_API_KEY"] == ENGINEER)
        )
        return ScriptResult(kw["operation"], 0, False, "a", "b", 0.1, "0" * 64)

    def submit(web_url: str, account: Any, **kw: Any) -> intake.SubmitResult:
        seen["question"] = kw["question"]
        seen["key"] = kw["key"]
        return intake.SubmitResult(
            201, "inc-1", "00000000-0000-0000-0000-000000000001", False, None
        )

    def judge(job: Any) -> int:
        seen["judge_env"] = dict(job["env"])
        Path(job["out"]).write_text(json.dumps({"choice": "A", "passed": True, "error_code": None,
                                                "attempts": 1, "prompt_sha256": "p", "request_bytes": 10}))  # fmt: skip
        return 0

    def start(name: str) -> Any:
        return lambda *a, **k: (
            seen.setdefault("env_" + name, a[0] if a else None),
            Started(name, lambda: None),
        )[1]

    return driver.Backends(
        start_pg=lambda d, p: _Pg(),
        stop_pg=lambda pg: None,
        start_web=start("web"),
        start_worker=start("worker"),
        run_script=run_script,
        es_admin=lambda u, k: shift.EsAdmin(url, k),
        submit=submit,
        run_state=lambda dsn, run: "completed",
        collect=lambda dsn, run: (
            {"report": REPORT, "outcome": {"report_available": True}},
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
        sleep=lambda s: None,
    )


def test_trial_end_to_end_with_fakes(
    tmp_path: Path,
    es: tuple[fake_es.FakeElasticsearch, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPSPILOT_LEDGER_DIR", str(tmp_path / "ledger"))
    store, url = es
    cfg = _cfg(tmp_path)
    cfg.es_url = url
    cfg.product_es["OPSPILOT_ES_URL"] = url
    cfg.questions = (QID,)
    _upstream(cfg.upstream)
    seen: dict[str, Any] = {}
    b = _backends(store, url, seen)
    report = driver.run_question(cfg, b, QID, [1])
    assert report["trials"][1]["judge"].get("choice") == "A", report
    summary = json.loads(cfg.summary_path(QID, 1).read_text())
    block = summary["holmes_eval"]
    assert block["shift"]["applied"] and block["shift"]["seconds"] % 60 == 0
    assert block["judge"]["choice"] == "A" and block["category"] == "ok"
    assert block["conversion"]["deshift_count"] == 1
    assert driver.trial_state(cfg, QID, 1) == "done"
    raw = cfg.summary_path(QID, 1).read_text()
    for secret in (ENGINEER, "model-key-x", "judge-key-x", "SYNTHETIC-ANSWER-ELEMENT"):
        assert secret not in raw
    assert (
        set(seen["judge_env"]) & {"DEEPSEEK_API_KEY", "ELASTICSEARCH_API_KEY"} == set()
    )
    assert seen["judge_env"]["ZAI_API_KEY"] == "judge-key-x"
    assert "ELASTICSEARCH_API_KEY" not in seen["env_worker"]
    assert seen["env_worker"]["DEEPSEEK_API_KEY"] == "model-key-x"
    assert all(engineer for _, engineer in seen["scripts"])
    # Judge input on disk is deshifted back to the original date.
    assert "2026-11-18" not in (cfg.trial_dir(QID, 1) / driver.JUDGE_INPUT).read_text()
    # Resume: a done trial is skipped.
    again = driver.run_question(cfg, b, QID, [1])
    assert again["skipped"] == [1]
    trial = driver.trial_record(summary)
    assert trial.passed and trial.category == "ok"


def test_void_line_retries_then_missing(
    tmp_path: Path,
    es: tuple[fake_es.FakeElasticsearch, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPSPILOT_LEDGER_DIR", str(tmp_path / "ledger"))
    store, url = es
    cfg = _cfg(tmp_path)
    cfg.es_url = url
    _upstream(cfg.upstream)
    b = _backends(store, url, {})
    clock = iter(
        datetime(2026, 11, 18, 12, tzinfo=UTC) + timedelta(seconds=200 * i)
        for i in range(1000)
    )
    b.now = lambda: next(clock)
    report = driver.run_question(cfg, b, QID, [1])
    assert report["trials"][1]["status"] == "missing"
    summary = json.loads(cfg.summary_path(QID, 1).read_text())
    assert summary["reason"] == "CHANCES_EXHAUSTED"
    assert [h["code"] for h in summary["holmes_eval"]["history"]] == [
        "SETUP_TO_DELIVERY_EXCEEDED"
    ] * 3
    assert driver.trial_state(cfg, QID, 1) == "missing"
    assert driver.trial_record(summary).category == "env_broken"


# -- PR #726 review: ES 8 wildcard deletes, batched shift, void clock ----------------


def test_fake_es_refuses_wildcard_delete_like_es8() -> None:
    store = fake_es.FakeElasticsearch(ENGINEER)
    _call(store, "PUT", "/app-1-a", {})
    status, out = _call(store, "DELETE", "/app-1-*")
    assert status == 400 and out["error"]["type"] == "illegal_argument_exception"
    assert _call(store, "DELETE", "/_all")[0] == 400
    assert _call(store, "DELETE", "/app-1-a")[0] == 200
    _call(store, "PUT", "/app-1-b", {})
    _call(
        store,
        "PUT",
        "/_cluster/settings",
        {"persistent": {"action.destructive_requires_name": "false"}},
    )
    assert _call(store, "DELETE", "/app-1-*")[0] == 200


def test_fake_es_date_detection_follows_es_defaults() -> None:
    store = fake_es.FakeElasticsearch(ENGINEER)
    _call(
        store,
        "POST",
        "/app-2-a/_doc",
        {"a": "2024-01-14 00:00:00", "b": "2024-01-14T00:00:00Z"},
    )
    _, mapping = _call(store, "GET", "/app-2-a/_mapping")
    props = mapping["app-2-a"]["mappings"]["properties"]
    assert props["a"]["type"] == "text" and props["b"]["type"] == "date"


def test_purge_by_name_and_fail_closed(
    es: tuple[fake_es.FakeElasticsearch, str],
) -> None:
    from scripts.holmes_evals.setup import purge_question_indices

    store, url = es
    names = [f"app-184-team-svc-2024.01.{i:02d}-{'x' * 40}" for i in range(120)]
    for name in names:
        _call(store, "PUT", f"/{name}", {})
    _call(store, "PUT", "/app-1840-keep", {})
    admin = shift.EsAdmin(url, ENGINEER)
    assert sorted(
        purge_question_indices(admin, "184_elasticsearch_index_explosion")
    ) == sorted(names)
    assert sorted(store.indices) == ["app-1840-keep"]
    assert all(
        len(",".join(g)) <= shift.URL_NAMES_CHARS for g in shift._name_groups(names)
    )
    assert len(shift._name_groups(names)) > 1

    class Stuck(shift.EsAdmin):
        def delete_indices(self, names: Any) -> None:
            return None

    _call(store, "PUT", "/app-184-left", {})
    with pytest.raises(shift.ShiftError) as err:
        purge_question_indices(
            Stuck(url, ENGINEER), "184_elasticsearch_index_explosion"
        )
    assert err.value.code == "PURGE_INCOMPLETE"


class _Counting(shift.EsAdmin):
    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        self.calls: list[str] = []

    def _call(
        self, method: str, path: str, body: Any = None, *, ndjson: bool = False
    ) -> Any:
        self.calls.append(f"{method} {path.split('?')[0].split('/')[-1]}")
        return super()._call(method, path, body, ndjson=ndjson)


def test_apply_shift_is_batched_across_indices(
    es: tuple[fake_es.FakeElasticsearch, str],
) -> None:
    store, url = es
    for i in range(1, 61):
        name = f"app-185-geo-eu-auth-2024.01.{i:02d}"
        _call(
            store,
            "PUT",
            f"/{name}",
            {"mappings": {"properties": {"@timestamp": {"type": "date"}}}},
        )
        _call(
            store,
            "POST",
            f"/{name}/_doc",
            {"@timestamp": f"2024-01-05T09:{i % 60:02d}:00Z", "m": "x"},
        )
    admin = _Counting(url, ENGINEER)
    now = datetime(2026, 11, 18, 19, tzinfo=UTC)
    _, record = shift.apply_shift(
        admin, "185_elasticsearch_cross_region_search", "q", now
    )
    assert record.applied and sum(i["rewritten"] for i in record.indices) == 60
    assert admin.calls.count("POST _bulk") == 1
    assert admin.calls.count("POST _refresh") == 1
    assert admin.calls.count("POST _forcemerge") == 1
    assert admin.calls.count("POST _search") <= 2
    _, out = _call(
        store,
        "POST",
        "/app-185-geo-*/_search",
        {"size": 0, "aggs": {"m": {"max": {"field": "@timestamp"}}}},
    )
    assert out["aggregations"]["m"]["value_as_string"].startswith("2026-11-18")


def test_void_clock_starts_after_shift(
    tmp_path: Path,
    es: tuple[fake_es.FakeElasticsearch, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPSPILOT_LEDGER_DIR", str(tmp_path / "ledger"))
    store, url = es
    cfg = _cfg(tmp_path)
    cfg.es_url = url
    _upstream(cfg.upstream)
    b = _backends(store, url, {})
    clock = [datetime(2026, 11, 18, 12, tzinfo=UTC)]

    def now() -> datetime:
        clock[0] += timedelta(seconds=1)
        return clock[0]

    real = driver.apply_shift

    def slow_shift(*a: Any, **k: Any) -> Any:
        out = real(*a, **k)
        clock[0] += timedelta(seconds=600)  # a 10-minute shift (184/185 scale)
        return out

    b.now = now
    monkeypatch.setattr(driver, "apply_shift", slow_shift)
    record = driver.run_trial(
        cfg, b, QID, questions.load_question(cfg.upstream, QID), 1
    )
    assert record["status"] == "completed" and record["history"] == []
    block = json.loads(cfg.summary_path(QID, 1).read_text())["holmes_eval"]
    assert block["shift_timing"]["seconds"] >= 600
    assert block["delivery"]["setup_to_delivery_seconds"] < 120
    assert block["delivery"]["data_end_to_delivery_seconds"] >= 60


def test_purge_failure_fails_closed_in_the_driver(
    tmp_path: Path,
    es: tuple[fake_es.FakeElasticsearch, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPSPILOT_LEDGER_DIR", str(tmp_path / "ledger"))
    store, url = es
    cfg = _cfg(tmp_path)
    cfg.es_url = url
    _upstream(cfg.upstream)

    class Stuck(shift.EsAdmin):
        def delete_indices(self, names: Any) -> None:
            return None

    b = _backends(store, url, {})
    b.es_admin = lambda u, k: Stuck(url, k)
    _call(store, "PUT", "/app-275-leftover", {})
    with pytest.raises(driver.RoundAbort) as err:
        driver.run_trial(cfg, b, QID, questions.load_question(cfg.upstream, QID), 1)
    assert err.value.code == "CLEANUP_FAILED"
    assert not cfg.summary_path(QID, 1).exists()
