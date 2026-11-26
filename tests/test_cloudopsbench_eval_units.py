"""Cloud-OpsBench adapter units (#871): question, conversion, subset, tools, attempt.

Synthetic inputs only: no benchmark checkout, no model, no network. Labels
and names come from the pinned lists (``scripts.cloudopsbench_eval.catalog``);
no case's answer is used.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from opspilot.tools.executor import TransportRequest, TransportTimeout
from opspilot.tools.outcomes import Window
from opspilot.tools.registry import ToolRegistry
from scripts.cloudopsbench_eval import answer, catalog, driver, question, subset
from scripts.cloudopsbench_eval import profile as cob
from scripts.cloudopsbench_eval.mcp_client import McpError
from scripts.itbench_lite.services import Started
from scripts.sregym_eval.question import QUESTION_MAX_CHARS

LONGEST_QUERY = "Service latency increased significantly"
VARIANTS = [
    ("boutique", "runtime"),
    ("boutique", "codedefect"),
    ("train-ticket", "service"),
]


# -- question (U2) ----------------------------------------------------------------


def test_template_hash_is_pinned_and_recomputable():
    blob = json.dumps(
        [
            question.QUESTION_TEMPLATE,
            catalog.ROOT_CAUSE_LIST,
            catalog.VALID_NODES,
            catalog.VALID_SERVICES,
            catalog.VALID_NAMESPACES,
        ]
    )
    digest = hashlib.sha256(blob.encode()).hexdigest()
    assert digest == question.QUESTION_TEMPLATE_SHA256 == question.template_sha256()


@pytest.mark.parametrize("system", ["boutique", "train-ticket"])
def test_longest_question_fits_and_carries_every_label_and_name(system):
    text = question.render_question(
        system, query=LONGEST_QUERY + ".", namespace="train-ticket"
    )
    assert len(text) <= QUESTION_MAX_CHARS
    assert len(catalog.LABELS) == 57
    for label in catalog.LABELS:
        assert f"- {label} (Requires Target:" in text
    for name in (*catalog.VALID_SERVICES[system], *catalog.VALID_NODES):
        assert name in text
    assert "Root cause: <label>; fault object: <kind>/<name>" in text


def test_idempotency_key_is_the_list_position_only():
    assert question.idempotency_key("r1", 7) == "cob-r1-c007-t1"


# -- conversion (U3) --------------------------------------------------------------


def _report(summary="", root_cause=None, claims=()):
    return {"summary": summary, "root_cause": root_cause, "claims": list(claims)}


def _rc(kind, name, namespace="boutique"):
    return {"kind": kind, "name": name, "namespace": namespace, "evidence_ids": ["e1"]}


@pytest.mark.parametrize(
    ("report", "system", "expected"),
    [
        # A Pod name: hash suffixes stripped; the longest service prefix wins.
        (
            _report(
                "Root cause: service_selector_mismatch; fault object: app/ts-order-service",
                _rc("Pod", "ts-order-other-service-7f9c8d6b5-x2k9q", "train-ticket"),
            ),
            "train-ticket",
            ("app/ts-order-other-service", "service_selector_mismatch",
             "root_cause_field", "summary_root_cause"),
        ),
        (
            _report("kubelet_unavailable on node/worker-02", None),
            "boutique",
            ("node/worker-02", "kubelet_unavailable", "summary_token", "summary_first"),
        ),
        # Node kind maps to node; an unlisted node is not a fault object.
        (
            _report("x", _rc("Node", "worker-09", None)),
            "boutique",
            ("", "", "not_found", "not_found"),
        ),
        (
            _report("x", _rc("Node", "Worker-01", None)),
            "boutique",
            ("node/worker-01", "", "root_cause_field", "not_found"),
        ),
        # ResourceQuota is its namespace; labels from fact/hypothesis claims
        # only, never from a rejected hypothesis.
        (
            _report(
                "Quota exhausted.",
                _rc("ResourceQuota", "compute-quota"),
                [
                    {"kind": "rejected_hypothesis", "text": "not pod_cpu_overload"},
                    {"kind": "hypothesis", "text": "namespace_cpu_quota_exceeded"},
                ],
            ),
            "boutique",
            ("namespace/boutique", "namespace_cpu_quota_exceeded",
             "root_cause_field", "claims_first"),
        ),
        # A whole-token match: pod_network_delay is not node_network_delay.
        (
            _report("Root cause: pod_network_delay", _rc("Deployment", "redis-cart")),
            "boutique",
            ("app/redis-cart", "pod_network_delay", "root_cause_field",
             "summary_root_cause"),
        ),
        # A name outside List B and no listed token: nothing is guessed.
        (
            _report("nacos is down", _rc("StatefulSet", "nacos-0", "train-ticket")),
            "train-ticket",
            ("", "", "not_found", "not_found"),
        ),
    ],
)  # fmt: skip
def test_conversion_rules(report, system, expected):
    got = answer.convert(report, system)
    assert (
        got.fault_object,
        got.root_cause,
        got.object_rule,
        got.label_rule,
    ) == expected


def test_case_file_is_what_the_scorer_reads_and_a_failed_case_has_no_answer():
    converted = answer.Converted("app/frontend", "pod_cpu_overload", "x", "y")
    data = answer.case_file(converted, [])
    top = data["final_answer"]["top_3_predictions"]
    assert top == [
        {"rank": 1, "fault_object": "app/frontend", "root_cause": "pod_cpu_overload"}
    ]
    assert answer.case_file(None, [])["final_answer"] is None
    record = answer.conversion_record(converted)
    assert "app/frontend" not in json.dumps(record)  # A6: rules only, no values


def test_trace_counts_separate_recorded_answers_from_synthetic_not_found():
    lines = [
        json.dumps({"tool_name": "GetAlerts", "output": "No active metric anomalies"}),
        json.dumps(
            {
                "tool_name": "DescribeResource",
                "output": 'Error from server (NotFound): pod "x" not found',
            }
        ),
        json.dumps(
            {
                "tool_name": "GetAppYAML",
                "output": "Error: YAML configuration for 'frontend' is not recorded.",
            }
        ),
        # Recorded output that happens to start with "Error" is a hit.
        json.dumps({"tool_name": "GetRecentLogs", "output": "Error: dial tcp refused"}),
        json.dumps({"tool_name": "GetResources", "output": "bad", "is_error": True}),
        "not json",
    ]
    events = answer.read_trace(lines)
    assert answer.cache_stats(events) == {
        "calls": 5,
        "hit": 2,
        "not_found": 1,
        "not_recorded": 1,
        "adapter_error": 1,
        "hit_rate": 0.4,
    }
    steps = answer.trajectory_steps(events)
    assert [s["action_name"] for s in steps][:2] == ["GetAlerts", "DescribeResource"]
    assert steps[4]["error"] == "bad"


# -- subset (U5) ------------------------------------------------------------------


def _strata():
    """58 synthetic strata of uneven size (1 to 47 cases), 713 cases."""
    strata = {}
    sizes = [1, 2, 3, 5, 8, 13, 21, 47] * 7 + [4, 9]
    categories = ("alpha", "beta", "gamma", "delta", "epsilon")
    for index, size in enumerate(sizes):
        system = "boutique" if index % 3 else "trainticket"
        for n in range(size):
            case = f"{system}/{categories[index % 5]}/{index * 100 + n}"
            strata[case] = f"s{index:02d}"
    return strata


def test_subset_is_stratified_complete_and_deterministic():
    strata = _strata()
    first = subset.select(strata, seed=871, target=100)
    again = subset.select(dict(reversed(list(strata.items()))), seed=871, target=100)
    assert first == again
    assert len(first) == 100 and len(set(first)) == 100
    assert set(strata[c] for c in first) == set(strata.values())  # >= 1 each
    assert subset.select(strata, seed=872, target=100) != first
    _, weights = subset.frozen(strata, first, seed=871, target=100)
    assert sum(r["full"] for r in weights["strata"]) == len(strata)
    assert sum(r["subset"] for r in weights["strata"]) == 100


def test_weighted_estimate_uses_full_set_shares():
    strata = {"a/x/1": "A", "a/x/2": "A", "a/x/3": "A", "a/y/4": "B"}
    assert subset.weighted_mean({"a/x/1": 1.0, "a/y/4": 0.0}, strata) == 0.75
    assert subset.weighted_mean({"a/x/1": 1.0}, strata) is None


# -- tool family (U1) ---------------------------------------------------------------


@pytest.mark.parametrize(("system", "category"), VARIANTS)
def test_face_keeps_the_benchmark_tools_and_arguments(system, category):
    tools = catalog.benchmark_tools(system, category)
    assert len(tools) == {"boutique": 12, "train-ticket": 10}[system]
    faces = cob.tool_schemas(system, category)
    for tool, face in zip(tools, faces, strict=True):
        function = face["function"]
        assert function["name"] == catalog.TOOL_NAMES[tool["name"]]
        assert function["description"].startswith(
            f"Cloud-OpsBench tool {tool['name']}."
        )
        assert cob.NOT_FOUND_NOTE in function["description"]  # A3
        schema = tool["inputSchema"]
        assert set(function["parameters"]["properties"]) == set(schema["properties"])
        assert function["parameters"].get("required", []) == schema.get("required", [])
    registry = ToolRegistry(cob.registrations(system, category))
    assert registry.revision
    assert cob.versions(system, category) == cob.versions(system, category)


def test_code_defect_cases_get_their_own_tool_set():
    plain = cob.versions("boutique", "runtime")["tool_schema_revision"]
    assert cob.versions("boutique", "codedefect")["tool_schema_revision"] != plain


class _Client:
    def __init__(self, reply=None, error=None):
        self.reply, self.error = reply, error

    def call_tool(self, name, arguments, timeout):
        if self.error:
            raise self.error
        return self.reply


def _request(tool="get_alerts"):
    window = Window.parse(
        {"start": "2026-11-26T00:00:00+00:00", "end": "2026-11-26T01:00:00+00:00"}
    )
    return TransportRequest(
        operation_id="s-t0",
        source=cob.SOURCE,
        verb="query",
        endpoint="http://127.0.0.1:18951/mcp",
        selector={},
        params={},
        window=window,
        timeout_seconds=5.0,
        max_result_bytes=1_000_000,
        credential_ref=cob.CREDENTIAL_REF,
        tool=tool,
    )


def test_transport_lines_adapter_errors_and_timeouts():
    ok = cob.CobTransport(_Client(("a\nb", False))).fetch(_request())
    assert json.loads(ok.body) == {"lines": ["a", "b"], "tool": "GetAlerts"}
    assert ok.source_status is None
    bad = cob.CobTransport(_Client(("Invalid tool arguments: x", True))).fetch(
        _request()
    )
    assert bad.source_status == "INVALID_ARGUMENTS"
    with pytest.raises(TransportTimeout):
        cob.CobTransport(_Client(error=McpError("MCP_TIMEOUT"))).fetch(_request())


# -- attempt (A2, A7) -------------------------------------------------------------------


def test_a_failed_attempt_still_writes_an_unanswered_case_file(tmp_path, monkeypatch):
    checkout = tmp_path / "cob"
    case = checkout / "benchmark" / "boutique" / "runtime" / "1"
    case.mkdir(parents=True)
    (case / "tool_cache.json").write_text("{}")
    (case / "metadata.json").write_text(
        json.dumps({"query": "Service abnormal restart.", "namespace": "boutique"})
    )
    monkeypatch.setattr(driver, "TMP_ROOT", tmp_path / "tmp")
    started = []

    def start_mcp(**kwargs):
        started.append(kwargs["env"])
        return Started("mcp", lambda: None, info={"tools": []})

    b = driver.Backends(
        start_mcp=start_mcp,
        start_pg=lambda *a: pytest.fail("PG must not start after TOOLS_MISMATCH"),
        port_in_use=lambda port: False,
    )
    cfg = driver.Config(round="t1", checkout=checkout, cob_python=Path("python3"))
    record = driver.Attempt(cfg, b, "boutique/runtime/1", 1).run()
    assert (record["status"], record["failure"]) == ("failed", "TOOLS_MISMATCH")
    # The server got the view, never the case directory.
    assert started[0]["CLOUDOPSBENCH_CASE_PATH"].endswith("/c001/view")
    path = tmp_path / "tmp/t1/answers/boutique/opspilot/runtime/1/1.json"
    assert json.loads(path.read_text())["final_answer"] is None
    assert (
        "boutique/runtime/1"
        not in (tmp_path / "tmp/t1/opspilot/c001/question.txt").read_text()
    )


def test_product_environments_refuse_a_dataset_path():
    with pytest.raises(driver.DriverError) as error:
        driver.check_product_env({"X": "/data/cob/benchmark"}, ["/data/cob"])
    assert error.value.code == "PRODUCT_ENV_LEAK"


@pytest.mark.parametrize(
    ("summary", "name", "agrees"),
    [
        (
            "Root cause: pod_cpu_overload; fault object: app/frontend",
            "frontend-1",
            True,
        ),
        (
            "Root cause: pod_cpu_overload; fault object: app/adservice",
            "frontend-1",
            False,
        ),
        ("Root cause: pod_cpu_overload", "frontend-1", None),
    ],
)
def test_object_agreement_is_recorded_without_changing_precedence(
    summary, name, agrees
):
    got = answer.convert(_report(summary, _rc("Pod", name)), "boutique")
    assert got.fault_object == "app/frontend"  # root_cause still wins
    record = answer.conversion_record(got)
    assert record["object_agrees_with_summary"] is agrees
    assert "frontend" not in json.dumps(record)


def test_score_maps_records_by_the_case_they_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, "TMP_ROOT", tmp_path / "tmp")
    cfg = driver.Config(round="t2", checkout=tmp_path, cob_python=Path("python3"))
    for seq, case in ((1, "boutique/runtime/2"), (2, "boutique/runtime/1")):
        path = cfg.round_dir / "opspilot" / f"c{seq:03d}" / driver.RECORD
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"case": case, "seq": seq, "status": "ok"}))
    records = driver._records(cfg, "opspilot", ["boutique/runtime/1"])
    assert records["boutique/runtime/1"]["seq"] == 2


def test_score_refuses_strata_that_differ_from_the_committed_file(tmp_path):
    cases = ["boutique/runtime/1", "boutique/runtime/2"]
    strata = dict(zip(cases, ["boutique/runtime/a", "boutique/runtime/b"], strict=True))
    sub = tmp_path / "subset.json"
    sub.write_text(json.dumps({"seed": 1, "target": 2, "cases": cases}))
    assert driver.check_strata(sub, strata, cases) == "no_committed_strata"
    _, weights = subset.frozen(strata, cases, seed=1, target=2)
    (tmp_path / "strata.json").write_text(json.dumps(weights))
    assert driver.check_strata(sub, strata, cases) == "match"
    moved = {**strata, "boutique/runtime/2": "boutique/runtime/a"}
    with pytest.raises(driver.DriverError) as error:
        driver.check_strata(sub, moved, cases)
    assert error.value.code == "STRATA_MISMATCH"
