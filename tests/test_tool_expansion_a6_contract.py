"""Independent contract tests for tabular Kubernetes queries (A6, #558).

The implementation is being written in another worktree.  This file is the
independent contract-test starting point and is intentionally red on ``main``
until ``kubernetes_tabular_query`` is registered and routed.

接缝假设（只列公开合同没有钉死、而测试必须采用的最小接缝）：

* ``otel-demo-ext`` continues to expose the existing A2 ``ToolProfile`` face,
  ``executor_factory`` and injected ``urllib.request.build_opener`` opener;
  the A2 contract helpers are reused for worker setup and its fake Kubernetes
  List responses.
* ``kubernetes_tabular_query`` is registered by the A2 Kubernetes registration
  seam (``opspilot.tools.kubernetes_core._registrations`` or an equivalent
  re-export), uses ``source="otel-demo"`` and the Kubernetes transport's
  ``k8s-ro`` credential slot, and is routed by its exact tool name.
* The public parameter ``columns`` is the upstream comma-separated string
  ``HEADER:FIELD_PATH,...``.  A successful adapter response is exposed through
  the executor's normal model view: ``header``, ``count``, ``scope`` and
  ``notes`` are view fields and the cell-array rows are ``content`` (the
  adapter's ``rows`` result path).  The test does not assume padding or a
  delimiter for the human-readable header line.
* The direct timeout test uses the existing A2 ``KubernetesTransport`` /
  ``TransportRequest`` seam, a short request deadline, and the existing
  ``TransportTimeout`` class.  The source has already been read when the
  regular-expression child is stopped, so ``source_contact`` is confirmed.
* A view-size refusal uses the existing executor's registered
  ``max_view_tokens`` check.  The whole view is refused; the test does not
  require a particular optional spilled-evidence record.

All synthetic credentials and identifiers use low-entropy placeholders.  No
real cluster, container, Docker, or full-suite pytest run is used.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from opspilot.tools import TransportError, TransportRequest, kubernetes_core
from opspilot.tools.executor import ToolRequest, TransportTimeout
from opspilot.tools.outcomes import Window
from opspilot.tools.profiles import PROFILE_ENV, select_profile
from tests.m1_tool_support import FakeClock
from tests.test_m1_otel_demo_contract import NOW, TARGET_ID
from tests.test_tool_expansion_a2_contract import (
    BASE_TOOL_NAMES,
    BASE_TOOL_SCHEMA_REVISION,
    K8S_API,
    K8S_NAMESPACE,
    K8S_TOKEN,
    OTEL_TOKEN,
    KubeOpener,
    _event,
    _executor,
    _headers,
    _list,
    _pod,
    _url,
    _view,
)

TABULAR_TOOL = "kubernetes_tabular_query"
A6_AUTHORIZED_WINDOW = Window(NOW - timedelta(hours=6), NOW)


@pytest.fixture(autouse=True)
def _synthetic_k8s_ca(tmp_path, monkeypatch):
    """Reuse the A2 worker seam without depending on a host CA path."""

    ca_file = tmp_path / "synthetic-ca.pem"
    ca_file.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("A2_TEST_K8S_CA_FILE", str(ca_file))


def _tabular_pod(
    name: str,
    phase: str = "Running",
    *,
    images: tuple[str, ...] = ("ghcr.io/checkout:v1",),
    namespace: str = K8S_NAMESPACE,
    blob: str | None = None,
) -> dict[str, object]:
    """A small Pod projection fixture with the fields A6 extracts."""

    item = _pod(name, phase, namespace=namespace, blob=blob)
    item["spec"] = {"containers": [{"image": image} for image in images]}
    return item


def _call(
    params: dict[str, object], *, index: int = 0, window: Window = A6_AUTHORIZED_WINDOW
) -> ToolRequest:
    return ToolRequest(
        step_id="a6-step",
        tool_index=index,
        tool_name=TABULAR_TOOL,
        target_ref=TARGET_ID,
        params=params,
        window=window.as_json(),
    )


def _params(
    *,
    kind: str = "pods",
    columns: str = "NAME:.metadata.name",
    **overrides: object,
) -> dict[str, object]:
    values: dict[str, object] = {"kind": kind, "columns": columns}
    values.update(overrides)
    return values


def _rows(outcome) -> list[list[object]]:
    rows = _view(outcome).get("content")
    assert isinstance(rows, list), _view(outcome)
    assert all(isinstance(row, list) for row in rows), rows
    return rows


def _header_labels(view: dict[str, object]) -> list[str]:
    """Normalize the contract's human header line for semantic assertions."""

    header = view.get("header")
    if isinstance(header, list):
        assert all(isinstance(item, str) for item in header), header
        return list(header)
    assert isinstance(header, str), view
    return header.split()


def _invalid_columns_refusal(opener: KubeOpener, params: dict[str, object]):
    """Read the pre-dispatch refusal through the existing transport seam."""

    transport = kubernetes_core.KubernetesTransport(
        credentials={kubernetes_core.CREDENTIAL_REF: K8S_TOKEN},
        opener=opener,
    )
    request = TransportRequest(
        operation_id="a6-invalid-columns-t0",
        source="otel-demo",
        verb="list",
        endpoint=K8S_API,
        selector={kubernetes_core.ENDPOINT_SELECTOR: K8S_API},
        params=params,
        window=A6_AUTHORIZED_WINDOW,
        timeout_seconds=1.0,
        max_result_bytes=4 * 1024 * 1024,
        credential_ref="k8s-ro",
        tool=TABULAR_TOOL,
        target_id=TARGET_ID,
    )
    return transport.fetch(request)


def _assert_invalid_columns(
    outcome, refusal, bad_column: str, opener: KubeOpener
) -> None:
    # The executor exposes only the standard parameter reason and no source
    # error for a request that was never sent.  The adapter refusal itself is
    # checked through its existing TransportResponse seam below.
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), (
        outcome.model_view
    )
    view = _view(outcome)
    assert view["content"] is None
    assert opener.requests == []
    assert refusal.sent is False
    refusal_text = refusal.body.decode("utf-8")
    refusal_record = f"{refusal.source_status}\n{refusal_text}"
    assert "INVALID_COLUMNS" in refusal_record
    assert (
        bad_column in refusal_record
        or bad_column.replace("\n", "\\n") in refusal_record
    )


def test_extension_surface_adds_tabular_query_without_pinning_future_tools():
    base = select_profile({PROFILE_ENV: "otel-demo"})
    ext = select_profile({PROFILE_ENV: "otel-demo-ext"})
    base_names = [
        item["function"]["name"] for item in base.face(FakeClock()).tool_schemas
    ]
    ext_names = [
        item["function"]["name"] for item in ext.face(FakeClock()).tool_schemas
    ]
    assert tuple(base_names) == BASE_TOOL_NAMES
    assert ext_names[: len(base_names)] == base_names
    assert set(ext_names) >= set(base_names) | {TABULAR_TOOL}
    assert base.versions()["tool_schema_revision"] == BASE_TOOL_SCHEMA_REVISION
    assert base.versions() == select_profile({PROFILE_ENV: "otel-demo"}).versions()


def test_schema_registration_and_description_pin_a6_contract():
    profile = select_profile({PROFILE_ENV: "otel-demo-ext"})
    schema = next(
        item["function"]
        for item in profile.face(FakeClock()).tool_schemas
        if item["function"]["name"] == TABULAR_TOOL
    )
    assert set(schema["parameters"]["properties"]) == {
        "kind",
        "columns",
        "namespace",
        "label_selector",
        "field_selector",
        "filter_pattern",
    }
    assert schema["parameters"]["required"] == ["kind", "columns"]
    assert schema["parameters"]["properties"]["columns"]["type"] == "string"

    registration = next(
        item for item in kubernetes_core._registrations() if item.name == TABULAR_TOOL
    )
    assert registration.source == "otel-demo"
    assert registration.verb == "list"
    assert registration.may_contain_secrets is True
    description_parts = [
        schema["description"],
        registration.description.returns,
        registration.description.window_format,
        registration.description.values_format,
        registration.description.limits,
        registration.description.cannot_prove,
    ]
    description = " ".join(description_parts).lower()
    assert "cannot prove" in description
    assert "incident" in description
    # This A6 contract deliberately removes the upstream claim that only the
    # requested fields are transmitted; the adapter reads the A2 resource list.
    assert "only requested fields" not in description
    assert set(registration.parameters) == {
        "kind",
        "columns",
        "namespace",
        "label_selector",
        "field_selector",
        "filter_pattern",
    }


def test_pod_name_status_image_columns_are_cell_arrays_and_one_operation(monkeypatch):
    opener = KubeOpener(
        [
            _list(
                [
                    _tabular_pod(
                        "checkout-0",
                        "Running",
                        images=("ghcr.io/checkout:v1", "ghcr.io/sidecar:v2"),
                    ),
                    _tabular_pod("checkout-1", "Pending"),
                ]
            )
        ]
    )
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(
            _params(
                columns="NAME:.metadata.name,STATUS:.status.phase,IMAGE:.spec.containers[*].image",
                namespace=K8S_NAMESPACE,
                label_selector="app=checkout",
                field_selector="status.phase!=Succeeded",
            )
        )
    )
    assert (outcome.status, outcome.reason) == ("ok", None), outcome.model_view
    view = _view(outcome)
    assert set(("header", "count", "scope", "notes")) <= set(view)
    assert _header_labels(view) == ["NAME", "STATUS", "IMAGE"]
    assert _rows(outcome) == [
        ["checkout-0", "Running", "ghcr.io/checkout:v1,ghcr.io/sidecar:v2"],
        ["checkout-1", "Pending", "ghcr.io/checkout:v1"],
    ]
    assert view["count"] == 2
    assert view["scope"]["namespace"] == K8S_NAMESPACE
    assert executor.operations_used == 1
    assert len(opener.requests) == 1
    request = opener.requests[0]
    assert request.get_method() == "GET"
    assert _headers(request).get("authorization") == f"Bearer {K8S_TOKEN}"
    assert OTEL_TOKEN not in json.dumps(_headers(request))
    query = parse_qs(urlsplit(_url(request)).query)
    assert query["labelSelector"] == ["app=checkout"]
    assert query["fieldSelector"] == ["status.phase!=Succeeded"]


def test_tabular_transport_does_not_fallback_from_k8s_ro_to_otel_credentials():
    transport = kubernetes_core.KubernetesTransport(
        credentials={"otel-demo-ro": OTEL_TOKEN}
    )
    request = TransportRequest(
        operation_id="a6-credential-t0",
        source="otel-demo",
        verb="list",
        endpoint=K8S_API,
        selector={kubernetes_core.ENDPOINT_SELECTOR: K8S_API},
        params=_params(),
        window=A6_AUTHORIZED_WINDOW,
        timeout_seconds=1.0,
        max_result_bytes=4 * 1024 * 1024,
        credential_ref="k8s-ro",
        tool=TABULAR_TOOL,
        target_id=TARGET_ID,
    )
    with pytest.raises(TransportError, match="CREDENTIAL_REF_UNKNOWN"):
        transport.fetch(request)


def test_missing_fields_array_expansion_and_index_out_of_bounds_are_stable(monkeypatch):
    opener = KubeOpener(
        [
            _list(
                [
                    _tabular_pod(
                        "checkout-0",
                        images=("ghcr.io/checkout:v1", "ghcr.io/sidecar:v2"),
                    )
                ]
            )
        ]
    )
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(
            _params(
                columns=(
                    "NAME:.metadata.name,ALL:.spec.containers[*].image,"
                    "FIRST:.spec.containers[0].image,OOB:.spec.containers[9].image,"
                    "MISSING:.status.reason"
                )
            )
        )
    )
    assert outcome.status == "ok", outcome.model_view
    assert _rows(outcome) == [
        [
            "checkout-0",
            "ghcr.io/checkout:v1,ghcr.io/sidecar:v2",
            "ghcr.io/checkout:v1",
            "<none>",
            "<none>",
        ]
    ]


def test_cluster_scoped_kind_ignores_namespace_and_records_note(monkeypatch):
    opener = KubeOpener([_list([{"metadata": {"name": "node-a"}}], kind="NodeList")])
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(
            _params(
                kind="nodes", namespace=K8S_NAMESPACE, columns="NAME:.metadata.name"
            )
        )
    )
    assert outcome.status == "ok", outcome.model_view
    assert _rows(outcome) == [["node-a"]]
    assert "ignored" in json.dumps(_view(outcome)["notes"]).lower()
    assert _view(outcome)["scope"]["cluster_scoped"] is True
    assert urlsplit(_url(opener.requests[0])).path.endswith("/api/v1/nodes")


def test_events_outside_the_wider_authorized_window_are_dropped(monkeypatch):
    inside = (A6_AUTHORIZED_WINDOW.start + timedelta(minutes=1)).isoformat()
    outside = (A6_AUTHORIZED_WINDOW.end + timedelta(minutes=1)).isoformat()
    opener = KubeOpener(
        [
            _list(
                [
                    _event("in-window", inside, reason="BackOff"),
                    _event("late", outside, reason="Failed"),
                ],
                kind="EventList",
            )
        ]
    )
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(_params(kind="events", columns="REASON:.reason", namespace=K8S_NAMESPACE))
    )
    assert outcome.status == "ok", outcome.model_view
    assert _rows(outcome) == [["BackOff"]]
    notes = json.dumps(_view(outcome)["notes"]).lower()
    assert "1" in notes and ("drop" in notes or "discard" in notes)


@pytest.mark.parametrize("kind", ["secrets", "Secrets", "secret", "Secret"])
def test_secret_variants_are_denied_without_a_cluster_request(monkeypatch, kind):
    opener = KubeOpener([])
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(_params(kind=kind)))
    assert (outcome.status, outcome.reason) == ("error", "RESOURCE_NOT_ALLOWED")
    assert opener.requests == []


def test_raw_source_over_four_mib_refuses_the_whole_tabular_result(monkeypatch):
    huge = _list([_tabular_pod("huge", blob="x" * (4 * 1024 * 1024))])
    opener = KubeOpener([huge])
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(_params(columns="NAME:.metadata.name")))
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert _view(outcome)["content"] is None


def test_more_than_twenty_columns_or_two_thousand_chars_is_invalid_before_request(
    monkeypatch,
):
    opener = KubeOpener([])
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    twenty_one = ",".join(f"C{i}:.metadata.name" for i in range(21))
    params = _params(columns=twenty_one)
    refusal = _invalid_columns_refusal(opener, params)
    outcome = executor.execute(_call(params))
    _assert_invalid_columns(outcome, refusal, twenty_one, opener)

    too_long = "H" * (2001 - len(":.metadata.name")) + ":.metadata.name"
    assert len(too_long) == 2001
    params = _params(columns=too_long)
    refusal = _invalid_columns_refusal(opener, params)
    outcome = executor.execute(_call(params, index=1))
    _assert_invalid_columns(outcome, refusal, too_long, opener)


def test_twenty_columns_and_two_thousand_chars_are_accepted_at_the_boundary(
    monkeypatch,
):
    opener = KubeOpener([_list([_tabular_pod("boundary")])])
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    twenty = ",".join(f"C{i}:.metadata.name" for i in range(20))
    outcome = executor.execute(_call(_params(columns=twenty)))
    assert outcome.status == "ok", outcome.model_view
    assert len(_rows(outcome)[0]) == 20

    opener = KubeOpener([_list([_tabular_pod("length-boundary")])])
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    exactly_two_thousand = "H" * (2000 - len(":.metadata.name")) + ":.metadata.name"
    assert len(exactly_two_thousand) == 2000
    outcome = executor.execute(_call(_params(columns=exactly_two_thousand)))
    assert outcome.status == "ok", outcome.model_view


@pytest.mark.parametrize("forbidden", ["'", "/", ";", "\n"])
def test_forbidden_column_characters_are_invalid_and_never_sent(monkeypatch, forbidden):
    bad = f"BAD:.metadata.name{forbidden}tail"
    opener = KubeOpener([])
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    params = _params(columns=bad)
    refusal = _invalid_columns_refusal(opener, params)
    outcome = executor.execute(_call(params))
    _assert_invalid_columns(outcome, refusal, bad, opener)


def test_malformed_column_echoes_the_offending_column(monkeypatch):
    bad = "BROKEN"
    opener = KubeOpener([])
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    params = _params(columns=bad)
    refusal = _invalid_columns_refusal(opener, params)
    outcome = executor.execute(_call(params))
    _assert_invalid_columns(outcome, refusal, bad, opener)


def test_filter_pattern_is_case_sensitive_and_matches_the_joined_row(monkeypatch):
    opener = KubeOpener(
        [
            _list(
                [
                    _tabular_pod("checkout-upper", "Running"),
                    _tabular_pod("checkout-lower", "running"),
                ]
            )
        ]
    )
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(
            _params(
                columns="NAME:.metadata.name,STATUS:.status.phase",
                filter_pattern=r"^checkout-upper.*Running$",
            )
        )
    )
    assert outcome.status == "ok", outcome.model_view
    assert _rows(outcome) == [["checkout-upper", "Running"]]
    assert _view(outcome)["count"] == 1


def test_no_row_count_cap_is_distinct_from_view_size_cap(monkeypatch):
    items = [_tabular_pod(f"p-{index}") for index in range(1100)]
    opener = KubeOpener([_list(items)])
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(_params(columns="NAME:.metadata.name")))
    assert outcome.status == "ok", outcome.model_view
    assert _view(outcome)["count"] == 1100
    assert len(_rows(outcome)) == 1100


def test_view_over_max_tokens_refuses_whole_result_without_partial_rows(monkeypatch):
    def high_entropy_blob(index: int) -> str:
        seed = f"a6-view-{index}".encode()
        chunks = []
        for _ in range(20):
            seed = hashlib.sha256(seed).digest()
            chunks.append(seed.hex())
        return "".join(chunks)[:600]

    items = [
        _tabular_pod(f"wide-{index}", blob=high_entropy_blob(index))
        for index in range(500)
    ]
    opener = KubeOpener([_list(items)])
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(_params(columns="NAME:.metadata.name,BLOB:.blob")))
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert _view(outcome)["content"] is None


def test_pathological_filter_pattern_is_hard_stopped_with_a2_timeout_class():
    if shutil.which("jq") is None:
        pytest.skip("local jq is required for the A2 Kubernetes transport seam")
    opener = KubeOpener([_list([_tabular_pod("regex", blob="a" * 20000 + "!")])])
    transport = kubernetes_core.KubernetesTransport(
        credentials={kubernetes_core.CREDENTIAL_REF: K8S_TOKEN},
        jq_path=shutil.which("jq"),
        opener=opener,
    )
    request = TransportRequest(
        operation_id="a6-timeout-t0",
        source="otel-demo",
        verb="list",
        endpoint=K8S_API,
        selector={kubernetes_core.ENDPOINT_SELECTOR: K8S_API},
        params=_params(columns="VALUE:.blob", filter_pattern=r"(a+)+$"),
        window=A6_AUTHORIZED_WINDOW,
        timeout_seconds=0.25,
        max_result_bytes=4 * 1024 * 1024,
        credential_ref="k8s-ro",
        tool=TABULAR_TOOL,
        target_id=TARGET_ID,
    )
    with pytest.raises(TransportTimeout) as raised:
        transport.fetch(request)
    assert raised.value.source_contact == "confirmed"
