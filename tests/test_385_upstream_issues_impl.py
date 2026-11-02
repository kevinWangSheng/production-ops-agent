"""F14 step 3 (#385): implementer's offline tests for the reproduction harness.

Covers the scripted endpoint (R7), the case table, the Run freeze, the
OpsPilot-side extraction (S7, L5), record validation (seams section 2) and
the mapping write-back (section 3). No docker, kind, GitHub or model calls;
the independent acceptance tests live under tests/acceptance/.
"""

from __future__ import annotations

import copy
import io
import json
import threading
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from scripts.holmes_baseline import capture, container, issues, mapping, repro
from scripts.holmes_baseline import config as hconfig
from scripts.holmes_baseline import run as hrun

ROOT = Path(__file__).resolve().parents[1]
MAPPING = ROOT / "docs/evidence/f14-upstream-mapping/mapping.json"
PIN = json.loads((ROOT / issues.PIN_PATH).read_text())


def _table_before_writeback() -> dict[str, Any]:
    """The step 2 table as main had it before step 3 wrote back (0089197e)."""
    import subprocess

    return json.loads(
        subprocess.check_output(
            [
                "git",
                "-C",
                str(ROOT),
                "show",
                "0089197e:docs/evidence/f14-upstream-mapping/mapping.json",
            ],
            text=True,
        )
    )


# --- scripted endpoint


def _script(tmp_path: Path, turns: list[dict[str, Any]]) -> Path:
    path = tmp_path / "script.json"
    path.write_text(json.dumps({"schema": capture.SCRIPT_SCHEMA, "turns": turns}))
    return path


def test_load_script_rejects_malformed_scripts(tmp_path: Path) -> None:
    good = [{"tool_calls": [{"name": "t", "arguments": {}}]}, {"final": "done"}]
    assert capture.load_script(str(_script(tmp_path, good))) == good
    for bad in (
        [],
        [{"tool_calls": [{"name": "t", "arguments": {}}]}],  # no final turn
        [{"final": "x", "tool_calls": []}],
        [{"tool_calls": [{"name": "t", "arguments": "{}"}]}, {"final": "x"}],
        [{"final": 3}],
    ):
        with pytest.raises(SystemExit):
            capture.load_script(str(_script(tmp_path, bad)))


def test_turn_index_follows_assistant_messages_and_caps_at_final() -> None:
    turns = [{"tool_calls": [{"name": "t", "arguments": {}}]}, {"final": "done"}]
    body: dict[str, Any] = {"messages": [{"role": "system"}, {"role": "user"}]}
    assert capture.scripted_turn(turns, body) == 0
    body["messages"] += [{"role": "assistant"}, {"role": "tool"}]
    assert capture.scripted_turn(turns, body) == 1
    body["messages"] += [{"role": "assistant"}, {"role": "user"}]
    assert capture.scripted_turn(turns, body) == 1


def test_scripted_payload_non_stream_and_stream() -> None:
    turn = {
        "tool_calls": [{"name": "q", "arguments": {"timeout": None, "query": "up"}}]
    }
    raw, kind = capture.scripted_payload(0, turn, "m", stream=False)
    assert kind == "application/json"
    choice = json.loads(raw)["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    call = choice["message"]["tool_calls"][0]
    assert call["function"]["name"] == "q"
    assert json.loads(call["function"]["arguments"]) == {"timeout": None, "query": "up"}
    raw, kind = capture.scripted_payload(1, {"final": "bye"}, "m", stream=True)
    assert kind == "text/event-stream"
    events = [e for e in raw.decode().split("\n\n") if e]
    assert events[-1] == "data: [DONE]"
    first = json.loads(events[0][len("data: ") :])
    assert first["choices"][0]["delta"]["content"] == "bye"
    raw, _ = capture.scripted_payload(0, turn, "m", stream=True)
    first = json.loads(raw.decode().split("\n\n")[0][len("data: ") :])
    assert first["choices"][0]["delta"]["tool_calls"][0]["index"] == 0
    last = json.loads(raw.decode().split("\n\n")[1][len("data: ") :])
    assert last["choices"][0]["finish_reason"] == "tool_calls"


def _serve(
    script: list[dict[str, Any]] | None, body: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    old = capture.SCRIPT
    capture.SCRIPT = script
    server = ThreadingHTTPServer(("127.0.0.1", 0), capture.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    out = io.StringIO()
    try:
        with redirect_stdout(out):
            thread.start()
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/chat/completions",
                data=json.dumps(body).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer x",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                reply = json.loads(response.read())
    finally:
        server.shutdown()
        server.server_close()
        capture.SCRIPT = old
    return reply, out.getvalue()


BODY = {
    "model": "deepseek-flash",
    "max_tokens": 65536,
    "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
    "tools": [{"type": "function", "function": {"name": "t"}}],
}


def test_default_mode_line_is_unchanged() -> None:
    reply, out = _serve(None, BODY)
    assert reply["choices"][0]["message"]["content"] == capture.REPLY
    line = json.loads(out.strip())
    # the pre-#385 line: no messages/tools, these keys only
    assert sorted(line) == [
        "body",
        "check",
        "header_names",
        "message_count",
        "path",
        "tool_names",
    ]
    assert line["body"] == {"max_tokens": 65536, "model": "deepseek-flash"}
    assert "authorization" not in line["header_names"]


def test_scripted_mode_prints_whole_request_without_authorization() -> None:
    turns = [{"tool_calls": [{"name": "t", "arguments": {"a": 1}}]}, {"final": "done"}]
    reply, out = _serve(turns, BODY)
    assert reply["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "t"
    line = json.loads(out.strip())
    assert line["kind"] == "scripted_request" and line["turn"] == 0
    assert line["body"]["messages"] == BODY["messages"]
    assert "authorization" not in line["header_names"]
    assert "Bearer" not in out


# --- container / helper argv


def test_run_args_unchanged_without_model_env() -> None:
    kw: dict[str, Any] = dict(
        network="n", egress_proxy="http://e:3128", config_path="/c", output_dir="/o"
    )
    plain = container.run_args("img", **kw, command=["ask", "q"])
    assert plain == container.run_args(
        "img", **kw, command=["ask", "q"], model_env=hconfig.MODEL_ENV
    )
    swapped = container.run_args(
        "img",
        **kw,
        command=["ask"],
        model_env=repro.CASES["max-output-token-reservation-set"].model_env,
    )
    assert "MAX_OUTPUT_TOKEN_RESERVATION=20000" in swapped
    assert not any(a.startswith("OVERRIDE_MAX_OUTPUT_TOKEN=") for a in swapped)
    assert swapped.count("-v") == plain.count("-v") == 2


def test_capture_args_mount_script_only_when_scripted(tmp_path: Path) -> None:
    plain = hrun.capture_args(None)
    assert plain == hrun.helper_args(
        hrun.CAPTURE, hrun.NETWORK, "/harness/capture.py", "8000"
    )
    script = tmp_path / "script.json"
    scripted = hrun.capture_args(script)
    assert f"{tmp_path}:/script:ro" in scripted
    assert scripted[-2:] == ["--script", "/script/script.json"]
    assert scripted.index(f"{tmp_path}:/script:ro") < scripted.index(hrun.HELPER_IMAGE)


def test_docker_context_defaults_to_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("F14_DOCKER_CONTEXT", raising=False)
    assert hrun.docker_env()["DOCKER_CONTEXT"] == hrun.kind_lab.CONTEXT
    monkeypatch.setenv("F14_DOCKER_CONTEXT", "colima")
    assert hrun.docker_env()["DOCKER_CONTEXT"] == "colima"


# --- case table


def test_cases_cover_every_run_slug_and_scripts_are_valid(tmp_path: Path) -> None:
    slugs = {case.slug for case in repro.CASES.values()}
    assert slugs == set(issues.SLUGS) - {"rollout-baseline-in-memory"}
    for case in repro.CASES.values():
        assert case.mode in ("scripted_model", "real_model")
        path = tmp_path / f"{case.name}.json"
        path.write_text(json.dumps(repro.script(case)))
        capture.load_script(str(path))
        content = repro.holmes_config(case, hrun.PROMETHEUS_URL, hrun.JAEGER_URL)
        changed = issues.canonical_sha256(content) != PIN["config"]["sha256"]
        assert changed == bool(case.prometheus_url or case.jaeger_url)
        assert changed == bool(case.config_reason)


def test_investigate_command_reads_the_alert_from_out() -> None:
    case = repro.CASES["raw-alert-real"]
    argv = repro.command(case, "ignored")
    assert argv[:2] == ["investigate", "alertmanager"]
    assert argv[argv.index("--alertmanager-url") + 1] == repro.ALERTMANAGER_URL
    assert "--alertmanager-file" not in argv
    file_argv = repro.command(repro.CASES["raw-alert-file-entry"], "ignored")
    assert file_argv[file_argv.index("--alertmanager-file") + 1] == "/out/alerts.json"
    alert = repro.alerts(0.0)[0]
    assert repro.CANARY_NOTE in alert["annotations"]["description"]
    assert alert["status"] == {"state": "active", "silencedBy": [], "inhibitedBy": []}
    assert alert["receivers"] and alert["updatedAt"]
    plain = repro.alerts(0.0, gettable=False)[0]
    assert plain["status"] == "firing" and "receivers" not in plain


# --- freezing


SYSTEM = "\n".join(
    [
        "# Available Toolsets",
        "jaeger: ... fetches one trace." + issues.FAILED_HEADING,
        "These toolsets are DISABLED (unconfigured) unless marked FAILED.",
        "* bash: shell",
        "* prometheus/metrics (FAILED, error: Failed to connect to http://127.0.0.1:9): metrics",
        "",
        "If you need a system you can't reach with your current tools: ...",
    ]
)


def test_snippets_keep_failed_lines_and_the_alert_paragraph() -> None:
    snippet = issues.failed_toolsets_snippet(SYSTEM)
    assert snippet is not None
    assert snippet.splitlines()[0] == issues.FAILED_HEADING
    assert "prometheus/metrics (FAILED" in snippet
    assert "bash" not in snippet
    user = f"intro\n {issues.ALERT_MARKER}\nalert body {repro.CANARY_NOTE}"
    alert = issues.alert_snippet(user)
    assert alert is not None and repro.CANARY_NOTE in alert


def test_sentence_rule_needs_source_and_unavailability_in_one_sentence() -> None:
    hit = issues.names_missing_source(
        "Checkout errors rose. Prometheus was unreachable (connection refused), so "
        "metrics are missing.",
        "prometheus",
    )
    assert hit["hit"] and "Prometheus was unreachable" in hit["excerpt"]
    assert not issues.names_missing_source(
        "Traces show failed Charge calls. The service is unavailable to users.",
        "jaeger",
    )["hit"]
    assert issues.names_missing_source("Jaeger 无法连接。", "jaeger")["hit"]
    long = "Prometheus unreachable " + "x" * 400
    assert len(issues.names_missing_source(long, "prometheus")["excerpt"]) == 240


def test_freeze_run_has_no_messages_or_answer_text() -> None:
    body = {
        **BODY,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "please investigate SECRET-ANSWER-MARK"},
        ],
    }
    output = {
        "result": "final SECRET-ANSWER-MARK",
        "messages": [{"role": "assistant", "content": "reasoning"}],
        "tool_calls": [
            {
                "tool_name": "execute_prometheus_instant_query",
                "toolset_name": "prometheus/metrics",
                "result": {
                    "status": "error",
                    "error": "e" * 900,
                    "params": {"q": 1},
                    "data": "d",
                },
            }
        ],
    }
    frozen = issues.freeze_run(
        run_id="r1",
        slug="upstream-2139-empty-result",
        case="2139-empty-result",
        mode="scripted_model",
        entry="ask",
        image_id=PIN["image_id"],
        config_sha256="0" * 64,
        started_at="2026-11-01T00:00:00Z",
        finished_at="2026-11-01T00:01:00Z",
        exit_code=0,
        timed_out=False,
        output=output,
        captured=[{"body": body}],
    )
    text = json.dumps(frozen)
    assert "SECRET-ANSWER-MARK" not in text and "reasoning" not in text
    assert frozen["schema"] == issues.RUN_SCHEMA
    assert frozen["requests"][0]["body_sha256"] == issues.canonical_sha256(body)
    assert frozen["requests"][0]["max_tokens"] == 65536
    assert "disabled_failed_toolsets" in frozen["requests"][0]["snippets"]
    assert len(frozen["tool_results"][0]["error"]) == 500
    assert frozen["answer_sha256"] is not None
    assert frozen["container_env"] == {}


def test_repro_checks_on_synthetic_results() -> None:
    def call(**result: Any) -> dict[str, Any]:
        return {"tool_name": "t", "result": result}

    found = repro.checks(
        repro.CASES["2139-empty-result"],
        {
            "tool_calls": [
                call(
                    status="error",
                    error="The prometheus query returned no result. Is the query correct?",
                )
            ]
        },
        [],
        "",
        [],
    )
    assert found["tool_checks"]["empty_result_as_error"]["hit"]
    found = repro.checks(
        repro.CASES["2356-prometheus-input"],
        {},
        [{"body": {"messages": [{"role": "system", "content": SYSTEM}]}}],
        "",
        [],
    )
    assert found["request_checks"]["failed_segment_names_source"]["hit"]
    found = repro.checks(
        repro.CASES["max-output-token-reservation-set"],
        {},
        [{"body": {"max_tokens": 120000, "messages": []}}],
        "",
        [],
    )
    assert not found["request_checks"]["max_tokens_equals_reservation"]["hit"]
    found = repro.checks(
        repro.CASES["raw-alert-real"],
        {
            "result": f"Done. {repro.CANARY}",
            "tool_calls": [call(status="success", params={"match": repro.CANARY})],
        },
        [],
        "",
        ["t holmes GET /api/v1/label/__name__/values 200"],
    )
    assert found["answer_checks"]["canary_in_answer"]["hit"]
    assert found["tool_checks"]["canary_in_tool_params"]["hit"]
    assert found["proxy_checks"]["metric_name_requests"]["hit"]
    scripted = repro.checks(repro.CASES["raw-alert-input"], {}, [], "", [])
    assert scripted["answer_checks"]["canary_in_answer"]["hit"] is False
    assert scripted["tool_checks"]["canary_in_tool_params"]["hit"] is False
    assert scripted["proxy_checks"]["metric_name_requests"]["hit"] is False


# --- OpsPilot side


def _first_request(data_sources: dict[str, Any]) -> bytes:
    context = json.dumps(
        {"type": "opspilot-evidence-context-v4", "data_sources": data_sources}
    )
    return json.dumps(
        {
            "model": "deepseek-flash",
            "messages": [
                {"role": "system", "content": "s"},
                {"role": "user", "content": context},
            ],
        }
    ).encode()


def test_first_request_fields_find_the_projection() -> None:
    sources = {
        "available": {"prometheus": ["metrics_range_query"]},
        "not_integrated": ["logs"],
    }
    raw = _first_request(sources)
    fields = issues.first_request_fields(raw, "prometheus")
    assert fields["data_sources"] == sources
    assert fields["first_request_sha256"] == issues.sha256_bytes(raw)
    assert fields["first_request_mentions_unreachable"] is False
    generic = _first_request(
        {**sources, "instruction": "When a metrics source is unavailable, say so."}
    )
    assert (
        issues.first_request_fields(generic, "prometheus")[
            "first_request_mentions_unreachable"
        ]
        is False
    )
    failed = _first_request(
        {**sources, "unreachable_at_start": {"prometheus": "refused"}}
    )
    assert (
        issues.first_request_fields(failed, "prometheus")[
            "first_request_mentions_unreachable"
        ]
        is True
    )
    with pytest.raises(SystemExit):
        issues.first_request_fields(json.dumps({"messages": []}).encode(), "prometheus")


def _side(
    holmes_k_report: int, ops_names: list[bool], ops_mentions: bool = False
) -> dict[str, Any]:
    runs = [
        {
            "first_request_mentions_unreachable": ops_mentions,
            "report_names_missing_source": n,
        }
        for n in ops_names
    ]
    return {
        "groups": {"prometheus": {"n": len(runs), "runs": runs}},
        "holmes": {"prometheus": {"k_input": 1, "k_report": holmes_k_report}},
    }


def test_q7_needs_both_gaps_on_the_prometheus_group() -> None:
    assert issues.q7(_side(1, [False, False, False]))["n1_triggered"] is True
    assert issues.q7(_side(0, [False, False, False]))["n1_triggered"] is False
    assert issues.q7(_side(3, [False, True, False]))["report_gap"] is False
    assert issues.q7(_side(1, [False] * 3, ops_mentions=True))["input_gap"] is False


# --- records and write-back


def _record(slug: str, findings: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    kind = issues.SLUGS[slug]
    number = issues.ISSUE_NUMBERS.get(slug)
    record = {
        "schema": issues.RECORD_SCHEMA,
        "slug": slug,
        "kind": kind,
        "upstream_ref": None
        if number is None
        else {
            "repo": issues.REPO,
            "number": number[0],
            "kind": number[1],
            "url": f"https://github.com/{issues.REPO}/{'pull' if number[1] == 'pr' else 'issues'}/{number[0]}",
            "state": "open",
            "checked_at": "2026-11-01T00:00:00Z",
        },
        "background_refs": [],
        "reporter_claims": [{"id": "C1", "quote": "claim text", "source": "x"}],
        "code_at_pin": ["holmes/main.py:L1-L2"],
        "code_at_master": {
            "sha": "0" * 40,
            "checked_at": "2026-11-01T00:00:00Z",
            "changed_files_relevant": [],
        },
        "related_fixes": [],
        "local_findings": findings,
        "image_id": PIN["image_id"],
        "configs": [],
        "reproduced_at": "2026-11-01T00:00:00Z",
        "own_findings": [],
        "opspilot_side": None,
        "mapping_rows": [],
    }
    return {**record, **extra}


def _run_file(directory: Path, slug: str, run_id: str, mode: str) -> str:
    (directory / "runs").mkdir(parents=True, exist_ok=True)
    (directory / "runs" / f"{run_id}.json").write_text(
        json.dumps(
            {
                "schema": issues.RUN_SCHEMA,
                "slug": slug,
                "image_id": PIN["image_id"],
                "mode": mode,
            }
        )
    )
    return f"runs/{run_id}.json"


def test_record_rules(tmp_path: Path) -> None:
    slug = "upstream-2376-null-timeout"
    ev = _run_file(tmp_path, slug, "r1", "scripted_model")
    finding = {
        "claim_id": "C1",
        "group": None,
        "verdict": "reproduced",
        "mode": "scripted_model",
        "runs": 1,
        "k": 1,
        "evidence": [ev],
        "note": "n",
    }
    rows = json.loads(MAPPING.read_text())["rows"]
    ok = _record(slug, [finding])
    assert issues.record_problems(slug, ok, tmp_path, PIN, rows) == []
    bad_cases: list[dict[str, Any]] = [
        {**finding, "k": 0},
        {**finding, "verdict": "not_reproduced"},
        {**finding, "evidence": ["runs/missing.json"]},
        {**finding, "mode": "source_only"},
        {**finding, "mode": "real_model"},  # needs 3 runs
        {**finding, "group": "prometheus"},
        {
            **finding,
            "verdict": "not_attempted",
            "mode": "source_only",
            "runs": 0,
            "k": None,
            "evidence": [],
            "note": " ",
        },
    ]
    for bad in bad_cases:
        assert issues.record_problems(
            slug, _record(slug, [bad]), tmp_path, PIN, rows
        ), bad
    assert issues.record_problems(
        slug, {**ok, "image_id": "sha256:x"}, tmp_path, PIN, rows
    )
    not_run = {
        **finding,
        "verdict": "not_attempted",
        "mode": "source_only",
        "runs": 0,
        "k": None,
        "evidence": [],
    }
    assert issues.record_problems(
        slug, _record(slug, [not_run]), tmp_path, PIN, rows
    ) == [f"{slug}: reproduced_at null iff nothing ran"]


def _candidate_records(
    verdicts: dict[str, str], root: Path
) -> dict[str, dict[str, Any]]:
    table = _table_before_writeback()
    records = {}
    for row in table["rows"]:
        candidate = row.get("candidate_defect")
        if candidate is None:
            continue
        slug = issues.slug_of(candidate["step3_ref"])
        verdict = verdicts[slug]
        if verdict == "not_attempted":
            finding = {
                "verdict": verdict,
                "mode": "source_only",
                "runs": 0,
                "k": None,
                "evidence": [],
            }
        else:
            finding = {
                "verdict": verdict,
                "mode": "scripted_model",
                "runs": 1,
                "k": int(verdict == "reproduced"),
                "evidence": [],
            }
        records[slug] = _record(
            slug,
            [{"claim_id": "C1", "group": None, "note": "n", **finding}],
            reporter_claims=[
                {"id": "C1", "quote": candidate["claim"], "source": "mapping.json"}
            ],
        )
        target = root / issues.record_path(slug)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}")
    return records


def test_writeback_follows_verdicts_and_keeps_step2_rules(tmp_path: Path) -> None:
    table = _table_before_writeback()
    verdicts = {
        "max-output-token-reservation": "reproduced",
        "raw-alert-in-prompt": "not_reproduced",
        "rollout-baseline-in-memory": "not_attempted",
    }
    records = _candidate_records(verdicts, tmp_path)
    records["upstream-2139-empty-result"] = _record(
        "upstream-2139-empty-result",
        [],
        mapping_rows=[
            {
                "feature": "F3",
                "step": 4,
                "mechanism": "空结果被报成错误（NO_DATA 分支走不到）",
            }
        ],
    )
    out = issues.writeback(table, records)
    assert issues.writeback(out, records) == out  # idempotent
    assert issues.writeback_problems(out, records) == []
    assert issues.writeback_problems(table, records)  # candidates left
    by_slug = {}
    for before, after in zip(table["rows"], out["rows"]):
        if "candidate_defect" in before:
            by_slug[issues.slug_of(before["candidate_defect"]["step3_ref"])] = (
                before,
                after,
            )
        elif issues.row_key(before) == (
            "F3",
            4,
            "空结果被报成错误（NO_DATA 分支走不到）",
        ):
            assert after["runtime_evidence"] == [
                *before["runtime_evidence"],
                issues.record_path("upstream-2139-empty-result"),
            ]
        else:
            assert after == before
    before, after = by_slug["max-output-token-reservation"]
    assert after["class"] == "confirmed_defect" and "candidate_defect" not in after
    assert after["runtime_evidence"][-2:] == [
        issues.record_path("max-output-token-reservation"),
        issues.PIN_PATH,
    ]
    assert "第 3 步：reproduced（1/1，scripted_model）" in after["note"]
    for slug in ("raw-alert-in-prompt", "rollout-baseline-in-memory"):
        before, after = by_slug[slug]
        assert after["class"] == before["class"]
        assert after["runtime_evidence"] == before["runtime_evidence"]
        assert after["step3_record"] == issues.record_path(slug)
    # the step 2 checks still hold on the written-back table
    for path in (
        "docs/evidence/f14-holmes-baseline/request-check.json",
        issues.PIN_PATH,
        "docs/evidence/f14-holmes-baseline/run1-tool-errors.json",
        "docs/evidence/f14-holmes-baseline/summary.json",
    ):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("{}")
    manifest = json.loads(
        (ROOT / "docs/evidence/f14-upstream-mapping/upstream-files.json").read_text()
    )
    rows = [after for _, after in by_slug.values()]
    for row in rows:
        problems = [
            p
            for p in mapping.row_problems(row, manifest, tmp_path)
            if "OpsPilot path" not in p and "not found in" not in p
        ]
        assert problems == [], problems


def test_writeback_refuses_without_a_record() -> None:
    table = _table_before_writeback()
    with pytest.raises(SystemExit):
        issues.writeback(copy.deepcopy(table), {})


def test_alertmanager_stub_serves_only_the_alerts(tmp_path: Path) -> None:
    from scripts.holmes_baseline import alertmanager_stub as stub

    stub.ALERTS = json.dumps(repro.alerts(0.0)).encode()
    server = ThreadingHTTPServer(("127.0.0.1", 0), stub.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    out = io.StringIO()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with redirect_stdout(out):
            thread.start()
            with urllib.request.urlopen(
                f"{base}/api/v2/alerts?active=true", timeout=10
            ) as r:
                assert json.loads(r.read())[0]["fingerprint"] == "f14s3canary0001"
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(f"{base}/api/v2/silences", timeout=10)
    finally:
        server.shutdown()
        server.server_close()
    assert [json.loads(line)["served"] for line in out.getvalue().splitlines()] == [
        True,
        False,
    ]


def test_alertmanager_helper_mounts_the_alert_dir_read_only(tmp_path: Path) -> None:
    args = hrun.alertmanager_args(tmp_path / "alerts.json")
    assert f"{tmp_path}:/alerts:ro" in args
    assert args[-3:] == ["/harness/alertmanager_stub.py", "9093", "/alerts/alerts.json"]
    assert "--read-only" in args and hrun.NETWORK in args


def test_first_request_tap_on_a_real_otel_demo_first_call(tmp_path: Path) -> None:
    """L5: the product's own first model call for an otel-demo Run carries the
    static data_sources projection, and the opt-in tap in m1_live_runner
    writes exactly those bytes plus the versions; no network, no model."""
    from datetime import datetime, timedelta, timezone

    from opspilot.investigation.context import (
        evidence_context_projection,
        initial_messages,
    )
    from opspilot.investigation.loop import ModelCall, serialized_request
    from opspilot.tools import otel_demo
    from opspilot.tools.profiles import PROFILE_ENV, select_profile
    from scripts import m1_live_runner

    env = {PROFILE_ENV: "otel-demo", "OPSPILOT_OTEL_PROMETHEUS_URL": repro.UNREACHABLE}
    profile = select_profile(env)
    run_id = "00000000-0000-4000-8000-000000000385"
    input = profile.face(m1_live_runner.SystemClock()).input_for(
        run_id=run_id,
        question=repro.QUESTION,
        target_id=otel_demo.TARGET_ID,
        deadline=datetime.now(timezone.utc) + timedelta(minutes=12),
        model_requests=100,
    )
    context = evidence_context_projection(input.evidence_context, run_id=run_id)
    messages, _ = initial_messages(input, evidence_context=context)
    call = ModelCall(
        messages=tuple(messages),
        tools=None,
        json_mode=False,
        max_tokens=65536,
        timeout_seconds=1,
    )

    class Inner:
        attempts: list[dict[str, Any]] = []

        def complete(self, c: Any) -> str:
            return "reply"

    path = tmp_path / "first.json"
    tap = m1_live_runner.FirstRequestTap(Inner(), path, profile.versions())  # type: ignore[arg-type]
    assert tap.complete(call) == "reply"
    tap.complete(
        ModelCall(
            messages=(), tools=None, json_mode=False, max_tokens=1, timeout_seconds=1
        )
    )
    assert path.read_bytes() == serialized_request(call)
    versions = json.loads((tmp_path / "first.json.versions.json").read_text())
    assert versions == profile.versions() and versions["tool_schema_revision"]
    fields = issues.first_request_fields(path.read_bytes(), "prometheus")
    assert fields["data_sources"]["available"] == otel_demo._data_sources()["available"]
    assert fields["first_request_mentions_unreachable"] is False


def test_input_gap_reads_only_the_data_sources_projection() -> None:
    """review-448: text outside the projection (a message, a tool description)
    must not count as telling the model a source was unreachable at start."""
    outside = json.dumps(
        {
            "messages": [{"role": "user", "content": "unreachable at start"}],
            "data_sources": {"available": {"prometheus": ["x"]}},
        }
    ).encode()
    assert (
        issues.first_request_fields(outside, "prometheus")[
            "first_request_mentions_unreachable"
        ]
        is False
    )
    inside = json.dumps(
        {
            "data_sources": {
                "available": {"prometheus": ["x"]},
                "instruction": "prometheus 开始时不可达",
            }
        }
    ).encode()
    assert (
        issues.first_request_fields(inside, "prometheus")[
            "first_request_mentions_unreachable"
        ]
        is True
    )
