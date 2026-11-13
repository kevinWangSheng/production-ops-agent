"""Implementer's unit tests for the A6 column query (#558).

Hermetic: the A2 implementer tests' fake API server stands in for the
cluster; the regex child is the real one. The independent contract tests
live in ``tests/test_tool_expansion_a6_contract.py``.
"""

from __future__ import annotations

import json
import shutil
from datetime import timedelta

import pytest

from opspilot.tools import kubernetes_core as k8s
from opspilot.tools import kubernetes_tabular as tab
from opspilot.tools import otel_demo, otel_demo_ext
from opspilot.tools.executor import (
    TransportRequest,
    TransportResultTooLarge,
    TransportTimeout,
)
from opspilot.tools.profiles import select_profile
from tests.m1_tool_support import FakeClock
from tests.test_554_kubernetes_core_impl import (
    API,
    K8S_TOKEN,
    WINDOW,
    FakeApi,
    _call,
    _executor,
    _list,
)

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("python3") is None,
    reason="jq and python3 on PATH are worker preconditions",
)

TOOL = tab.TABULAR_TOOL
COLUMNS = "NAME:.metadata.name,STATUS:.status.phase,IMAGE:.spec.containers[*].image"


def _pod(name, phase="Running", images=("app:1",), namespace="otel-demo"):
    return {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "managedFields": [{"manager": "kubelet"}],
        },
        "spec": {
            "containers": [{"name": f"c{i}", "image": x} for i, x in enumerate(images)]
        },
        "status": {"phase": phase},
    }


def _transport(api: FakeApi) -> k8s.KubernetesTransport:
    return k8s.KubernetesTransport(
        credentials={k8s.CREDENTIAL_REF: K8S_TOKEN},
        jq_path=shutil.which("jq") or "jq",
        opener=api,  # type: ignore[arg-type]
    )


def _request(params: dict, *, timeout: float = 10.0) -> TransportRequest:
    return TransportRequest(
        operation_id="step-1-t0",
        source=otel_demo.SOURCE,
        verb="list",
        endpoint="http://127.0.0.1:19090",
        selector={k8s.ENDPOINT_SELECTOR: API},
        params=params,
        window=WINDOW,
        timeout_seconds=timeout,
        max_result_bytes=tab.MAX_RESULT_BYTES,
        credential_ref=otel_demo.CREDENTIAL_REF,
        tool=TOOL,
    )


def _body(response) -> dict:
    return json.loads(response.body)


# -- column syntax ---------------------------------------------------------------


@pytest.mark.parametrize(
    "spec",
    [
        "NAME:.metadata.name",
        "NAME:{.metadata.name}",
        "NAME:metadata.name",
        "IMAGE:.spec.containers[0].image",
        "IMAGES:.spec.containers[*].image",
        "HASH:.metadata.labels.pod-template-hash",
        " NAME : .metadata.name , PHASE:.status.phase",
        ",".join(f"C{i}:.metadata.name" for i in range(tab.MAX_COLUMNS)),
    ],
)
def test_supported_column_specifications_parse(spec):
    assert isinstance(tab.parse_columns(spec), list)


@pytest.mark.parametrize(
    ("spec", "problem"),
    [
        ("", "empty"),
        (None, "empty"),
        ("A:.a" + "b" * tab.MAX_COLUMNS_CHARS, "too_long"),
        (",".join(f"C{i}:.a" for i in range(tab.MAX_COLUMNS + 1)), "too_many_columns"),
        ("A:.metadata.labels['app']", "forbidden_character"),
        ("A:.metadata.labels.kubernetes.io/arch", "forbidden_character"),
        ("A:.a;B:.b", "forbidden_character"),
        ("A:.a\nB:.b", "forbidden_character"),
        ("A:.a\tB", "forbidden_character"),
        ("NAME", "not_header_colon_path"),
        ("A:.a:b", "not_header_colon_path"),
        (":.metadata.name", "empty_header"),
        ("A:..name", "unsupported_path"),
        ("A:.", "unsupported_path"),
        ("A:.a.", "unsupported_path"),
        ("A:.items[-1]", "unsupported_path"),
        ("A:.items[0:2]", "not_header_colon_path"),
        ("A:.items[?(@.x)]", "unsupported_path"),
        ('A:.metadata["name"]', "unsupported_path"),
        ("A:{.metadata.name", "unsupported_path"),
        ("A:.a,", "not_header_colon_path"),
    ],
)
def test_unsupported_columns_name_their_problem_and_column(spec, problem):
    found = tab.parse_columns(spec)
    assert isinstance(found, tab.ColumnProblem)
    assert found.problem == problem


def test_cells_follow_the_upstream_printer():
    obj = {
        "metadata": {"name": "a", "labels": {"app": "x"}},
        "spec": {"containers": [{"image": "i1"}, {"image": "i2"}], "empty": []},
        "status": {"ready": True, "restarts": 3, "gone": None},
    }

    def one(path):
        columns = tab.parse_columns(f"X:{path}")
        assert isinstance(columns, list)
        return tab.cell(obj, columns[0])

    assert one(".metadata.name") == "a"
    assert one(".spec.containers[*].image") == "i1,i2"
    assert one(".spec.containers[1].image") == "i2"
    assert one(".spec.containers[5].image") == tab.MISSING
    assert one(".spec.empty[*]") == tab.MISSING
    assert one(".status.gone") == tab.MISSING
    assert one(".status.nothing") == tab.MISSING
    assert one(".metadata.name.deeper") == tab.MISSING
    assert one(".status.ready") == "true"
    assert one(".status.restarts") == "3"
    assert one(".metadata.labels") == '{"app":"x"}'


# -- transport -------------------------------------------------------------------


def test_lists_name_status_image_of_one_namespace():
    api = FakeApi(
        {
            "/api/v1/namespaces/otel-demo/pods": [
                _list(
                    "Pod",
                    [
                        _pod("checkout-1", "Running", ("checkout:1", "envoy:1")),
                        _pod("cart-1", "CrashLoopBackOff", ("cart:2",)),
                    ],
                )
            ]
        }
    )
    response = _transport(api).fetch(
        _request({"kind": "pods", "namespace": "otel-demo", "columns": COLUMNS})
    )
    body = _body(response)
    assert body["header"] == ["NAME", "STATUS", "IMAGE"]
    assert body["rows"] == [
        ["checkout-1", "Running", "checkout:1,envoy:1"],
        ["cart-1", "CrashLoopBackOff", "cart:2"],
    ]
    assert body["count"] == 2
    assert body["scope"]["namespace"] == "otel-demo"
    assert body["scope"]["all_namespaces"] is False
    assert set(body) == {"header", "rows", "count", "scope", "notes"}
    assert response.source_start_at is None and response.source_end_at is None
    assert api.query()["limit"] == ["500"]
    assert response.view_fields is not None
    assert response.view_fields["header"] == body["header"]


def test_invalid_columns_are_refused_before_sending_with_the_column_echoed():
    api = FakeApi()
    response = _transport(api).fetch(
        _request({"kind": "pods", "columns": "NAME:.metadata.name,BAD:.a[?(@.x)]"})
    )
    assert response.sent is False
    assert response.source_status == "INVALID_COLUMNS"
    assert _body(response)["column"] == "BAD:.a[?(@.x)]"
    assert api.requests == []
    registration = tab.tabular_registrations(max_window_seconds=86400)[0]
    assert registration.classify("INVALID_COLUMNS") == "INVALID_PARAMS"


def test_secrets_are_refused_first_whatever_the_columns():
    api = FakeApi()
    response = _transport(api).fetch(_request({"kind": "Secret", "columns": "bad"}))
    assert response.source_status == "RESOURCE_NOT_ALLOWED"
    assert api.requests == []


def test_cluster_scoped_kind_ignores_namespace_with_a_note():
    api = FakeApi(
        {"/api/v1/nodes": [_list("Node", [{"metadata": {"name": "node-1"}}])]}
    )
    response = _transport(api).fetch(
        _request(
            {"kind": "nodes", "namespace": "otel-demo", "columns": "N:.metadata.name"}
        )
    )
    body = _body(response)
    assert body["rows"] == [["node-1"]]
    assert body["scope"]["cluster_scoped"] is True
    assert any("cluster-scoped" in note for note in body["notes"])


def _event(name, moment, reason="BackOff"):
    return {
        "metadata": {"name": name, "namespace": "otel-demo"},
        "reason": reason,
        "lastTimestamp": moment.isoformat().replace("+00:00", "Z"),
    }


def test_events_use_the_a2_window_filter_and_returned_rows_bound_source_time():
    inside_early = WINDOW.start + timedelta(hours=1)
    inside_late = WINDOW.start + timedelta(hours=2)
    api = FakeApi(
        {
            "/api/v1/events": [
                _list(
                    "Event",
                    [
                        _event("old", WINDOW.start - timedelta(hours=1)),
                        _event("e1", inside_early, "Pulled"),
                        _event("e2", inside_late),
                    ],
                )
            ]
        }
    )
    response = _transport(api).fetch(
        _request(
            {
                "kind": "events",
                "columns": "NAME:.metadata.name,REASON:.reason",
                "filter_pattern": "BackOff",
            }
        )
    )
    body = _body(response)
    assert body["rows"] == [["e2", "BackOff"]]
    assert any("1 events outside" in note for note in body["notes"])
    assert all("jq" not in note for note in body["notes"])
    assert response.source_start_at == response.source_end_at == inside_late


def test_filter_pattern_is_case_sensitive_and_skips_the_header():
    api = FakeApi(
        {
            "/api/v1/pods": [
                _list(
                    "Pod",
                    [_pod("a", "Running"), _pod("b", "running"), _pod("c", "Failed")],
                )
            ]
        }
    )
    response = _transport(api).fetch(
        _request({"kind": "pods", "columns": COLUMNS, "filter_pattern": "^a   Running"})
    )
    body = _body(response)
    assert [row[0] for row in body["rows"]] == ["a"]
    assert body["count"] == 1
    assert response.view_fields is not None
    assert response.view_fields["filter_mode"] == "regex"
    assert response.view_fields["rows_removed_by_filter"] == 2


def test_a_pattern_that_does_not_compile_is_a_noted_substring():
    api = FakeApi({"/api/v1/pods": [_list("Pod", [_pod("a[1"), _pod("b")])]})
    response = _transport(api).fetch(
        _request(
            {"kind": "pods", "columns": "N:.metadata.name", "filter_pattern": "a["}
        )
    )
    body = _body(response)
    assert body["rows"] == [["a[1"]]
    assert any("substring" in note for note in body["notes"])


def test_a_pathological_pattern_is_hard_stopped():
    api = FakeApi({"/api/v1/pods": [_list("Pod", [_pod("a" * 30000 + "!")])]})
    with pytest.raises(TransportTimeout) as raised:
        _transport(api).fetch(
            _request(
                {
                    "kind": "pods",
                    "columns": "N:.metadata.name",
                    "filter_pattern": "(a+)+$",
                },
                timeout=0.5,
            )
        )
    assert raised.value.source_contact == "confirmed"


def test_over_the_read_limit_is_refused_whole():
    big = [_pod(f"p{i}", images=("x" * 1000,)) for i in range(5000)]
    api = FakeApi({"/api/v1/pods": [_list("Pod", big)]})
    with pytest.raises(TransportResultTooLarge):
        _transport(api).fetch(_request({"kind": "pods", "columns": COLUMNS}))


# -- executor and profile --------------------------------------------------------


def test_executor_adopts_the_table_and_reports_invalid_columns(monkeypatch, tmp_path):
    api = FakeApi({"/api/v1/pods": [_list("Pod", [_pod("a", "Pending")])]})
    executor = _executor(monkeypatch, tmp_path, api)
    ok = executor.execute(_call(TOOL, {"kind": "pods", "columns": COLUMNS}))
    assert ok.status == "ok", ok.model_view
    assert ok.model_view["content"] == [["a", "Pending", "app:1"]]
    assert ok.model_view["header"] == ["NAME", "STATUS", "IMAGE"]
    assert ok.model_view["count"] == 1
    assert ok.model_view["source_start_at"] is None
    bad = executor.execute(_call(TOOL, {"kind": "pods", "columns": "A:.a/b"}))
    assert (bad.status, bad.reason) == ("error", "INVALID_PARAMS")
    api.pages["/api/v1/pods"] = 403
    denied = executor.execute(_call(TOOL, {"kind": "pods", "columns": COLUMNS}))
    assert (denied.status, denied.reason) == ("error", "SOURCE_FORBIDDEN")
    assert len(api.requests) == 2


def test_only_the_extended_profile_offers_the_tool():
    ext = select_profile({"OPSPILOT_TOOL_PROFILE": "otel-demo-ext"})
    base = select_profile({"OPSPILOT_TOOL_PROFILE": "otel-demo"})
    names = {item["function"]["name"] for item in ext.face(FakeClock()).tool_schemas}
    assert TOOL in names
    base_names = {
        item["function"]["name"] for item in base.face(FakeClock()).tool_schemas
    }
    assert TOOL not in base_names
    context = ext.face(FakeClock()).evidence_context("a6-run")
    assert TOOL in context["data_sources"]["available"]["kubernetes"]
    assert TOOL in otel_demo_ext.TOOL_NAMES
    assert TOOL in {item.name for item in k8s._registrations()}
    description = next(
        item["function"]["description"]
        for item in otel_demo_ext.TOOL_SCHEMAS
        if item["function"]["name"] == TOOL
    )
    assert "kubernetes_jq_query" in description
    assert "Only requested fields are transmitted" not in description
