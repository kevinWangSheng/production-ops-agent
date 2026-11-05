"""S7b acceptance: the HolmesGPT dataset runner (offline part).

Independent acceptance tests (written without the implementation). Contract
sources: F1 closure prereview §1 C (same read-only access, tool-shape
difference disclosed, no extra Holmes tool), R15 (budget aligned with the
frozen OpsPilot values, no extra toolsets), §2.2 S7 (question entry only,
same proxy, 503 through the Holmes egress proxy, missing telemetry by URL),
§3.3 item 2 (Holmes denominator = question-entry scenarios only); M3
closure contract r1 V2-R4/V2-R7/V2-R12/V2-R15; M3 direction Q11 (Holmes:
only "read-only, not over-privileged" and "investigation ran to the end").

Entry point the implementation provides: module ``scripts.eval_holmes_dataset``

``select_scenarios(loaded: Sequence[tuple[ScenarioSpecV2, AnswerV2]]) -> list[tuple[ScenarioSpecV2, AnswerV2]]``
    The scenarios Holmes runs: question entry only, input order kept.

``holmes_plan(spec, answer, *, submitted_at: datetime) -> Mapping[str, Any]``
    One Holmes Run of one scenario, submitted at ``submitted_at`` (aware
    UTC). Keys::

        "prompt": str                 # what ``holmes ask`` receives
        "window": {"start": datetime, "end": datetime}
        "config": dict                # the --config file content
        "model_env": dict[str, str]   # container model environment
        "wall_seconds": float         # harness kill deadline
        "model_transport": None | "always_503" | "first_503"
        "upstream": {"kind": "hash", "value": str}

    Raises ``ValueError`` for an alertmanager-entry scenario.

``result_row(spec, answer, *, attempt_id: str, plan, holmes_output: Mapping, proxy_log_lines: Sequence[str]) -> dict``
    The upstream row of one Holmes attempt for the S7a summary. Keys
    include ``system`` (``"holmes"``), ``scenario_id``, ``attempt_id``,
    ``final_state``, ``report_text`` (``None`` without a report),
    ``checks`` (exactly ``read_only`` and ``completed``) and ``upstream``
    (the plan's). ``holmes_output`` is Holmes' ``--json-output-file`` dict
    plus the harness keys ``exit_code``/``timed_out``/``error``; the proxy
    log line format is ``scripts.holmes_baseline.project.proxied_queries_ok``'s.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from opspilot.evaluation.model import load_scenarios_v2
from opspilot.investigation import limits
from opspilot.investigation.loop import ACCEPTED_RESPONSE_MODEL
from scripts import eval_holmes_dataset as s7b
from scripts import eval_live_lab
from scripts.holmes_baseline import config as hconfig

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "evals/datasets/dev-v2"
SUBMITTED = datetime(2026, 11, 10, 18, 30, 0, tzinfo=UTC)
JAEGER = hconfig.JAEGER_TOOLSET
PROMETHEUS = "prometheus/metrics"


@pytest.fixture(scope="module")
def loaded() -> dict[str, Any]:
    return {
        spec.scenario_id: (spec, answer) for spec, answer in load_scenarios_v2(DATASET)
    }


def _by(loaded: dict[str, Any], **want: str) -> list[tuple[Any, Any]]:
    out = []
    for spec, answer in loaded.values():
        params = [dict(i.params, kind=i.kind) for i in answer.injectors]
        if "entry" in want and spec.entry != want["entry"]:
            continue
        if "category" in want and answer.category != want["category"]:
            continue
        if "polarity" in want and answer.polarity != want["polarity"]:
            continue
        if "source" in want and not any(
            p["kind"] == "source_unreachable" and p.get("source") == want["source"]
            for p in params
        ):
            continue
        out.append((spec, answer))
    assert out, want
    return out


def _healthy(loaded: dict[str, Any]) -> tuple[Any, Any]:
    for spec, answer in loaded.values():
        if spec.entry == "question" and not any(
            i.kind in ("source_unreachable", "model_transport")
            for i in answer.injectors
        ):
            return spec, answer
    raise AssertionError("no plain question scenario")


# --- which scenarios (V2-R12, §3.3 item 2)


def test_only_question_entry_scenarios_run(loaded: dict[str, Any]) -> None:
    items = list(loaded.values())
    selected = s7b.select_scenarios(items)
    ids = [spec.scenario_id for spec, _ in selected]
    want = [spec.scenario_id for spec, _ in items if spec.entry == "question"]
    assert ids == want
    assert len(want) < len(items)  # the dataset has alert-entry scenarios
    for spec, answer in _by(loaded, entry="alertmanager"):
        with pytest.raises(ValueError):
            s7b.holmes_plan(spec, answer, submitted_at=SUBMITTED)


# --- same question and window (V2-R4, V2-R15)


def _utc(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_same_question_and_window(loaded: dict[str, Any]) -> None:
    window = timedelta(seconds=eval_live_lab.WINDOW_SECONDS)
    prompts = set()
    for spec, answer in s7b.select_scenarios(list(loaded.values())):
        plan = s7b.holmes_plan(spec, answer, submitted_at=SUBMITTED)
        assert spec.stimulus.question in plan["prompt"]
        assert plan["window"]["end"] == SUBMITTED
        assert plan["window"]["end"] - plan["window"]["start"] == window
        assert _utc(SUBMITTED - window) in plan["prompt"]
        assert _utc(SUBMITTED) in plan["prompt"]
        prompts.add(plan["prompt"])
    # every question scenario shares the one question (V2-R4)
    assert len(prompts) == 1


def test_prompt_leaks_nothing_from_the_answer(loaded: dict[str, Any]) -> None:
    for spec, answer in s7b.select_scenarios(list(loaded.values())):
        prompt = s7b.holmes_plan(spec, answer, submitted_at=SUBMITTED)["prompt"]
        lowered = prompt.lower()
        assert answer.category.replace("_", " ") not in lowered
        assert answer.category not in lowered
        for injector in answer.injectors:
            flag = injector.params.get("flag")
            if flag:
                assert str(flag).lower() not in lowered
            message = injector.params.get("message")
            if message:
                assert str(message) not in prompt


# --- same read-only tools, aligned budget (§1 C, R15)


def test_same_read_only_tools_for_every_scenario(loaded: dict[str, Any]) -> None:
    for spec, answer in s7b.select_scenarios(list(loaded.values())):
        config = s7b.holmes_plan(spec, answer, submitted_at=SUBMITTED)["config"]
        toolsets = config["toolsets"]
        enabled = {name for name, body in toolsets.items() if body.get("enabled")}
        assert enabled == {PROMETHEUS, JAEGER}
        for name in hconfig.BUILTIN_TOOLSETS:
            if name != PROMETHEUS:
                assert toolsets[name]["enabled"] is False, name
        for endpoint in toolsets[JAEGER]["config"]["endpoints"]:
            assert endpoint["methods"] == ["GET"]


def test_budget_matches_the_frozen_opspilot_values(loaded: dict[str, Any]) -> None:
    for spec, answer in s7b.select_scenarios(list(loaded.values())):
        plan = s7b.holmes_plan(spec, answer, submitted_at=SUBMITTED)
        assert plan["config"]["max_steps"] == limits.MAX_MODEL_REQUESTS_PER_RUN
        assert plan["config"]["model"].endswith(ACCEPTED_RESPONSE_MODEL)
        assert plan["model_env"]["OVERRIDE_MAX_OUTPUT_TOKEN"] == str(
            limits.MAX_OUTPUT_TOKENS
        )
        assert plan["model_env"]["OVERRIDE_MAX_OUTPUT_TOKEN"] == "65536"
        assert plan["wall_seconds"] == limits.RUN_WALL_SECONDS


# --- harness-side faults (S7: missing telemetry by URL, 503 at egress)


def _jaeger_hosts(config: dict[str, Any]) -> list[str]:
    return [
        host
        for endpoint in config["toolsets"][JAEGER]["config"]["endpoints"]
        for host in endpoint["hosts"]
    ]


def _prometheus_url(config: dict[str, Any]) -> str:
    return str(config["toolsets"][PROMETHEUS]["config"]["prometheus_url"])


def test_missing_telemetry_points_only_that_source_elsewhere(
    loaded: dict[str, Any],
) -> None:
    healthy = s7b.holmes_plan(*_healthy(loaded), submitted_at=SUBMITTED)["config"]
    for spec, answer in _by(loaded, source="jaeger"):
        config = s7b.holmes_plan(spec, answer, submitted_at=SUBMITTED)["config"]
        assert _jaeger_hosts(config) != _jaeger_hosts(healthy)
        assert _prometheus_url(config) == _prometheus_url(healthy)
    for spec, answer in _by(loaded, source="prometheus"):
        config = s7b.holmes_plan(spec, answer, submitted_at=SUBMITTED)["config"]
        assert _prometheus_url(config) != _prometheus_url(healthy)
        assert _jaeger_hosts(config) == _jaeger_hosts(healthy)


def test_model_transport_fault_uses_the_scenario_mode(loaded: dict[str, Any]) -> None:
    for spec, answer in s7b.select_scenarios(list(loaded.values())):
        modes = [
            i.params.get("mode")
            for i in answer.injectors
            if i.kind == "model_transport"
        ]
        plan = s7b.holmes_plan(spec, answer, submitted_at=SUBMITTED)
        assert plan["model_transport"] == (modes[0] if modes else None)
    assert {
        s7b.holmes_plan(s, a, submitted_at=SUBMITTED)["model_transport"]
        for s, a in _by(loaded, category="model_failure")
    } == {"always_503", "first_503"}


# --- upstream dimension (V2-R7)


def test_upstream_dimension_is_one_value_naming_the_pinned_commit(
    loaded: dict[str, Any],
) -> None:
    values = set()
    for spec, answer in s7b.select_scenarios(list(loaded.values())):
        upstream = s7b.holmes_plan(spec, answer, submitted_at=SUBMITTED)["upstream"]
        assert upstream["kind"] == "hash"
        assert hconfig.UPSTREAM_COMMIT in upstream["value"]
        assert re.search(
            r"[0-9a-f]{64}", upstream["value"].replace(hconfig.UPSTREAM_COMMIT, "")
        )
        values.add(upstream["value"])
    # harness-side fault URLs do not move the frozen dimension between rounds
    assert len(values) == 1


# --- result rows (Q11 Holmes items)

GET_OK = "2026-11-10T18:31:00Z holmes GET /api/v1/query_range?x=1 200 120 900 0.02"
POST = (
    "2026-11-10T18:31:05Z holmes POST /api/v1/admin/tsdb/delete_series 403 80 10 0.01"
)


def _row(
    loaded: dict[str, Any], output: dict[str, Any], lines: list[str]
) -> dict[str, Any]:
    spec, answer = _healthy(loaded)
    plan = s7b.holmes_plan(spec, answer, submitted_at=SUBMITTED)
    return s7b.result_row(
        spec,
        answer,
        attempt_id="att-1",
        plan=plan,
        holmes_output=output,
        proxy_log_lines=lines,
    )


def test_completed_read_only_row(loaded: dict[str, Any]) -> None:
    spec, _ = _healthy(loaded)
    output = {
        "result": "payment Charge errors cause the checkout failures",
        "tool_calls": [{"tool_name": "execute_prometheus_range_query"}],
        "num_llm_calls": 3,
        "exit_code": 0,
        "timed_out": False,
    }
    row = _row(loaded, output, [GET_OK])
    assert row["system"] == "holmes"
    assert row["scenario_id"] == spec.scenario_id
    assert row["attempt_id"] == "att-1"
    assert row["final_state"] == "completed"
    assert "payment Charge errors" in row["report_text"]
    assert row["checks"] == {"read_only": True, "completed": True}
    assert (
        row["upstream"]["value"] and hconfig.UPSTREAM_COMMIT in row["upstream"]["value"]
    )


def test_non_get_request_fails_read_only(loaded: dict[str, Any]) -> None:
    output = {"result": "done", "exit_code": 0, "timed_out": False}
    row = _row(loaded, output, [GET_OK, POST])
    assert row["checks"]["read_only"] is False


def test_wall_clock_kill_is_not_a_completed_investigation(
    loaded: dict[str, Any],
) -> None:
    output = {"exit_code": -9, "timed_out": True, "error": "killed"}
    row = _row(loaded, output, [GET_OK])
    assert row["final_state"] == "budget_exhausted"
    assert row["report_text"] is None
    assert row["checks"] == {"read_only": True, "completed": False}


def test_failed_run_without_report(loaded: dict[str, Any]) -> None:
    output = {"exit_code": 1, "timed_out": False, "error": "503 Service Unavailable"}
    row = _row(loaded, output, [])
    assert row["final_state"] == "failed"
    assert row["report_text"] is None
    assert row["checks"]["completed"] is False
