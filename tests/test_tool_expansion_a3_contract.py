"""Independent contract tests for Pod logs (A3, issue #555).

The implementation is being written in another worktree.  This file is
therefore intentionally red on the main branch until ``fetch_pod_logs`` is
registered and routed by the implementation worktree.

接缝假设（只列实现者必须采用、而公开合同没有逐字钉死的最小接缝）：

* ``otel-demo-ext`` continues to use the A2 ``ToolProfile`` face,
  ``executor_factory`` and injected ``urllib.request.build_opener`` opener;
  the A2 contract helpers are reused here instead of inventing another worker
  setup.
* ``fetch_pod_logs`` is registered by ``opspilot.tools.kubernetes_core`` (or
  an equivalent module re-exported through that A2 registration seam) and is
  routed by its exact tool name through the Kubernetes transport.  Its
  ``TransportRequest`` verb is ``logs`` and its credential slot is ``k8s-ro``.
* The fake Pod response uses the ordinary Kubernetes ``spec.containers``
  list.  Each log endpoint returns the Kubernetes ``timestamps=true`` plain
  text stream; the view exposes ``content`` as a list of JSON-like row
  mappings.  The tests locate timestamp/container/instance/text by their
  conventional keys where present, but do not require unrelated view-field
  names beyond the A3 contract.
* ``POD_NOT_FOUND`` is a fixed detail visible in the parameter-error view
  (the executor's outer reason remains ``INVALID_PARAMS``), and namespace and
  pod values are echoed there.
* For the hard-stop test the A2 direct ``KubernetesTransport.fetch`` seam is
  used with a deliberately short ``TransportRequest.timeout_seconds``; a
  pathological regex raises the same ``TransportTimeout`` class as A2 jq.
  This keeps the test bounded without changing the registered production
  timeout or reaching a real cluster.
* An alert Run carries its default query window through the existing
  ``AlertAnchor``/``ToolFace.input_for`` seam; a non-alert Run has no such
  default and therefore uses the authorized end minus one hour.

All synthetic credentials and identifiers are low-entropy placeholders.  No
real cluster, container, Docker, or full-suite pytest run is used.
"""

from __future__ import annotations

import io
import json
import shutil
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from opspilot.investigation.inputs import AlertAnchor
from opspilot.tools import kubernetes_core
from opspilot.tools.executor import ToolRequest, TransportRequest, TransportTimeout
from opspilot.tools.outcomes import Window
from opspilot.tools.profiles import PROFILE_ENV, select_profile
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import NOW, TARGET_ID
from tests.test_tool_expansion_a2_contract import (
    AUTHORIZED_WINDOW,
    BASE_TOOL_NAMES,
    BASE_TOOL_SCHEMA_REVISION,
    K8S_API,
    K8S_NAMESPACE,
    K8S_TOKEN,
    OTEL_TOKEN,
    FakeStore,
    _call,
    _env,
    _executor,
    _headers,
    _lease,
    _reply,
    _url,
)

LOG_TOOL = "fetch_pod_logs"
POD = "checkout-0"
START = AUTHORIZED_WINDOW.start + timedelta(hours=1)
END = AUTHORIZED_WINDOW.end - timedelta(hours=1)


@pytest.fixture(autouse=True)
def _synthetic_k8s_ca(tmp_path, monkeypatch):
    """Mirror the A2 contract seam without relying on another module fixture."""
    ca_file = tmp_path / "synthetic-ca.pem"
    ca_file.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("A2_TEST_K8S_CA_FILE", str(ca_file))


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _log_body(*lines: str) -> bytes:
    return ("\n".join(lines) + ("\n" if lines else "")).encode()


def _pod(
    name: str = POD, *, namespace: str = K8S_NAMESPACE, containers=("app",)
) -> bytes:
    return json.dumps(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {"containers": [{"name": item} for item in containers]},
            "status": {"phase": "Running"},
        },
        separators=(",", ":"),
    ).encode()


class PodLogOpener:
    """Fake Kubernetes API for one Pod GET and its per-stream log GETs."""

    def __init__(
        self,
        *,
        pod_body: bytes | None = None,
        pod_status: int | None = None,
        streams: dict[tuple[str, bool], bytes | int] | None = None,
    ):
        self.pod_body = _pod() if pod_body is None else pod_body
        self.pod_status = pod_status
        self.streams = dict(streams or {})
        self.requests: list[object] = []
        self.timeouts: list[float | None] = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        url = _url(request)
        parts = urlsplit(url)
        if (
            parts.path.endswith(f"/pods/{POD}")
            or "/pods/" in parts.path
            and not parts.path.endswith("/log")
        ):
            value: bytes | int = (
                self.pod_status if self.pod_status is not None else self.pod_body
            )
        elif parts.path.endswith("/log"):
            query = parse_qs(parts.query)
            container = query.get("container", [""])[0]
            previous = query.get("previous", ["false"])[0].lower() == "true"
            value = self.streams.get((container, previous), b"")
        else:
            raise AssertionError(f"unexpected Kubernetes request: {url}")
        if isinstance(value, int):
            body = json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "Status",
                    "reason": "NotFound" if value == 404 else "Synthetic",
                    "message": "synthetic status",
                    "details": {"name": POD},
                }
            ).encode()
            raise urllib.error.HTTPError(
                url, value, "synthetic status", {}, io.BytesIO(body)
            )
        return _reply(value)

    def query(self, index: int) -> dict[str, list[str]]:
        return parse_qs(urlsplit(_url(self.requests[index])).query)


def _params(**overrides) -> dict[str, object]:
    values: dict[str, object] = {"pod_name": POD, "namespace": K8S_NAMESPACE}
    values.update(overrides)
    return values


def _request(
    params: dict[str, object], *, window: Window = AUTHORIZED_WINDOW, index: int = 0
) -> ToolRequest:
    return ToolRequest(
        step_id="a3-step",
        tool_index=index,
        tool_name=LOG_TOOL,
        target_ref=TARGET_ID,
        params=params,
        window=window.as_json(),
    )


def _view(outcome):
    assert outcome.model_view is not None
    return outcome.model_view


def _rows(outcome) -> list[object]:
    rows = _view(outcome).get("content")
    assert isinstance(rows, list), _view(outcome)
    return rows


def _row_value(row: object, *names: str):
    assert isinstance(row, dict), row
    for name in names:
        if name in row:
            return row[name]
    return None


def _row_text(row: object) -> str:
    value = _row_value(row, "text", "line", "message", "content")
    assert isinstance(value, str), row
    return value


def _row_time(row: object) -> datetime:
    value = _row_value(row, "timestamp", "time", "ts", "source_time")
    assert isinstance(value, str), row
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _row_blob(row: object) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True)


def _alert_executor(monkeypatch, opener):
    """Build the normal extension executor with an AlertAnchor input."""
    env = _env()
    profile = select_profile(env)
    lease = _lease()
    store = FakeStore(lease, deadline=NOW + timedelta(minutes=10))
    clock = FakeClock(start=NOW)
    sink = RecordingSink()
    alert = AlertAnchor(
        received_at=AUTHORIZED_WINDOW.end,
        starts_at=AUTHORIZED_WINDOW.end - timedelta(hours=3),
        original=_rfc3339(AUTHORIZED_WINDOW.end - timedelta(hours=3)),
    )
    alert_window = Window(*alert.frame)
    model_input = profile.face(clock).input_for(
        run_id=str(lease.run_id),
        question="Why is checkout degraded?",
        target_id=TARGET_ID,
        deadline=NOW + timedelta(minutes=10),
        model_requests=8,
        alert=alert,
    )
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: opener)
    factory = profile.executor_factory(store, sink, clock)
    return factory(lease, model_input), alert, alert_window


def test_extension_face_is_a_superset_and_otel_demo_is_byte_stable():
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
    assert set(ext_names) >= set(base_names) | {LOG_TOOL}
    assert base.versions()["tool_schema_revision"] == BASE_TOOL_SCHEMA_REVISION
    assert base.versions() == select_profile({PROFILE_ENV: "otel-demo"}).versions()


def test_pod_logs_are_available_and_move_the_source_out_of_not_integrated():
    profile = select_profile({PROFILE_ENV: "otel-demo-ext"})
    face = profile.face(FakeClock())
    names = {item["function"]["name"] for item in face.tool_schemas}
    assert LOG_TOOL in names
    context = face.evidence_context("a3-run")
    sources = context["data_sources"]
    assert LOG_TOOL in sources["available"]["kubernetes"]
    assert "pod_logs" not in sources["not_integrated"]


def test_log_schema_and_registration_pin_the_public_surface():
    profile = select_profile({PROFILE_ENV: "otel-demo-ext"})
    schema = next(
        item["function"]
        for item in profile.face(FakeClock()).tool_schemas
        if item["function"]["name"] == LOG_TOOL
    )
    assert set(schema["parameters"]["properties"]) == {
        "pod_name",
        "namespace",
        "start_time",
        "end_time",
        "limit",
        "filter",
        "exclude_filter",
    }
    assert schema["parameters"]["required"] == ["pod_name", "namespace"]
    text = schema["description"].lower()
    assert "absolute" in text and "rfc3339" in text
    assert "100" in text and "1000" in text
    assert "cannot" in text or "unknown" in text

    registration = next(
        item for item in kubernetes_core._registrations() if item.name == LOG_TOOL
    )
    assert registration.source == "otel-demo"
    assert registration.verb == "logs"
    assert registration.may_contain_secrets is True
    description = registration.description
    assert description.returns and description.window_format
    assert description.values_format and description.limits and description.cannot_prove
    assert (
        "cannot" in description.cannot_prove.lower()
        or "unknown" in description.cannot_prove.lower()
    )


def test_multiple_containers_read_current_and_previous_once_sort_and_count_one_operation(
    monkeypatch,
):
    streams = {
        ("app", False): _log_body(
            f"{_rfc3339(START + timedelta(hours=1))} app-old",
            f"{_rfc3339(START + timedelta(hours=3))} app-new",
        ),
        ("app", True): _log_body(
            f"{_rfc3339(START + timedelta(hours=2))} app-previous",
        ),
        ("sidecar", False): _log_body(
            f"{_rfc3339(START + timedelta(hours=4))} sidecar-new",
        ),
        ("sidecar", True): _log_body(
            f"{_rfc3339(START + timedelta(minutes=30))} sidecar-previous",
        ),
    }
    opener = PodLogOpener(pod_body=_pod(containers=("app", "sidecar")), streams=streams)
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(
            LOG_TOOL,
            _params(start_time=_rfc3339(START), end_time=_rfc3339(END), limit=4),
        )
    )
    assert (outcome.status, outcome.reason) == ("ok", None), outcome.model_view
    assert len(opener.requests) == 5  # 1 Pod GET + 2 streams for each of 2 containers.
    assert executor.operations_used == 1
    assert len(_rows(outcome)) == 4
    assert [_row_time(row) for row in _rows(outcome)] == sorted(
        _row_time(row) for row in _rows(outcome)
    )
    text = " ".join(_row_text(row) for row in _rows(outcome))
    assert "app-new" in text and "sidecar-new" in text
    assert all(
        _row_value(row, "container", "container_name") in {"app", "sidecar"}
        for row in _rows(outcome)
    )
    assert {
        bool(_row_value(row, "previous", "is_previous")) for row in _rows(outcome)
    } == {False, True}
    for index, request in enumerate(opener.requests):
        assert request.get_method() == "GET"
        assert _headers(request).get("authorization") == f"Bearer {K8S_TOKEN}"
        if index:
            query = parse_qs(urlsplit(_url(request)).query)
            assert query.get("timestamps") == ["true"]
            assert (
                "limitBytes" in query
                and 0 < int(query["limitBytes"][0]) <= 4 * 1024 * 1024
            )


def test_previous_instance_and_not_started_400_are_notes_only(monkeypatch):
    streams = {
        ("app", False): _log_body(f"{_rfc3339(START)} app-current"),
        ("app", True): 400,
        ("sidecar", False): 400,
        ("sidecar", True): _log_body(
            f"{_rfc3339(START + timedelta(minutes=1))} sidecar-previous"
        ),
    }
    opener = PodLogOpener(pod_body=_pod(containers=("app", "sidecar")), streams=streams)
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(LOG_TOOL, _params(start_time=_rfc3339(START), end_time=_rfc3339(END)))
    )
    assert (outcome.status, outcome.reason) == ("ok", None), outcome.model_view
    notes = json.dumps(_view(outcome), ensure_ascii=False).lower()
    assert "previous" in notes and "sidecar" in notes
    content = json.dumps(_rows(outcome), ensure_ascii=False)
    assert "app-current" in content and "sidecar-previous" in content


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "SOURCE_FORBIDDEN"),
        (403, "SOURCE_FORBIDDEN"),
        (429, "SOURCE_UNAVAILABLE"),
        (500, "SOURCE_UNAVAILABLE"),
        (503, "SOURCE_UNAVAILABLE"),
    ],
)
def test_pod_log_source_statuses_map_to_fixed_reasons(monkeypatch, status, reason):
    opener = PodLogOpener(pod_status=status)
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(LOG_TOOL, _params()))
    assert (outcome.status, outcome.reason) == ("error", reason)
    assert len(opener.requests) == 1


def test_pod_404_is_invalid_params_with_pod_not_found_detail(monkeypatch):
    opener = PodLogOpener(pod_status=404)
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(LOG_TOOL, _params()))
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    view = json.dumps(_view(outcome), ensure_ascii=False)
    assert "POD_NOT_FOUND" in view
    assert K8S_NAMESPACE in view and POD in view


def test_all_empty_streams_are_no_data(monkeypatch):
    opener = PodLogOpener(pod_body=_pod(containers=("app",)), streams={})
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(LOG_TOOL, _params(start_time=_rfc3339(START), end_time=_rfc3339(END)))
    )
    assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA")
    assert _rows(outcome) == []


def test_read_cap_keeps_rows_marks_incomplete_and_suggests_a_later_start(monkeypatch):
    current = _log_body(
        *(
            f"{_rfc3339(START + timedelta(seconds=index))} " + "x" * 1700
            for index in range(2200)
        )
    )
    previous = _log_body(
        *(
            f"{_rfc3339(START + timedelta(hours=2, seconds=index))} " + "y" * 1700
            for index in range(300)
        )
    )
    assert len(current) < 4 * 1024 * 1024 < len(current) + len(previous)
    opener = PodLogOpener(
        pod_body=_pod(containers=("app",)),
        streams={("app", False): current, ("app", True): previous},
    )
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(
            LOG_TOOL,
            _params(start_time=_rfc3339(START), end_time=_rfc3339(END), limit=2),
        )
    )
    assert outcome.status == "ok", outcome.model_view
    view = _view(outcome)
    assert view["incomplete"] is True
    assert _rows(outcome)
    notes = json.dumps(view, ensure_ascii=False).lower()
    assert any(
        word in notes
        for word in ("4 mib", "4mib", "read limit", "since", "start", "later")
    )


def test_late_lines_are_dropped_and_source_bounds_stay_in_the_authorized_window(
    monkeypatch,
):
    late = END + timedelta(minutes=1)
    opener = PodLogOpener(
        streams={
            ("app", False): _log_body(
                f"{_rfc3339(START)} inside",
                f"{_rfc3339(late)} late",
            )
        }
    )
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(LOG_TOOL, _params(start_time=_rfc3339(START), end_time=_rfc3339(END)))
    )
    assert outcome.status == "ok"
    assert "inside" in json.dumps(_rows(outcome))
    assert "late" not in json.dumps(_rows(outcome))
    assert outcome.evidence is not None
    assert outcome.evidence.source_end_at <= AUTHORIZED_WINDOW.end
    assert (
        "drop" in json.dumps(_view(outcome)).lower()
        or "outside" in json.dumps(_view(outcome)).lower()
    )


def test_untimestamped_lines_are_dropped_and_counted_in_notes(monkeypatch):
    opener = PodLogOpener(
        streams={
            ("app", False): _log_body(
                "this line has no timestamp",
                f"{_rfc3339(START)} timestamped",
            )
        }
    )
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(LOG_TOOL, _params(start_time=_rfc3339(START), end_time=_rfc3339(END)))
    )
    assert outcome.status == "ok"
    assert "timestamped" in json.dumps(_rows(outcome))
    assert "no timestamp" not in json.dumps(_rows(outcome)).lower()
    notes = json.dumps(_view(outcome)).lower()
    assert "timestamp" in notes and ("drop" in notes or "untimed" in notes)


def test_long_lines_are_cut_to_2000_chars_and_marked(monkeypatch):
    opener = PodLogOpener(
        streams={("app", False): _log_body(f"{_rfc3339(START)} " + "z" * 2500)}
    )
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(LOG_TOOL, _params(start_time=_rfc3339(START), end_time=_rfc3339(END)))
    )
    assert outcome.status == "ok"
    row = _rows(outcome)[0]
    text = _row_text(row)
    assert len(text) <= 2000
    assert "trunc" in _row_blob(row).lower() or "…" in text or "..." in text


def test_filters_are_case_insensitive_and_match_line_content_only(monkeypatch):
    opener = PodLogOpener(
        pod_body=_pod(containers=("target-container",)),
        streams={
            ("target-container", False): _log_body(
                f"{_rfc3339(START)} healthy",
                f"{_rfc3339(START + timedelta(minutes=1))} TARGET outage",
                f"{_rfc3339(START + timedelta(minutes=2))} ignore TARGET",
            )
        },
    )
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(
            LOG_TOOL,
            _params(
                start_time=_rfc3339(START),
                end_time=_rfc3339(END),
                filter="target",
                exclude_filter="ignore",
            ),
        )
    )
    assert outcome.status == "ok"
    content = json.dumps(_rows(outcome), ensure_ascii=False).lower()
    assert "target outage" in content
    assert "healthy" not in content and "ignore target" not in content


def test_invalid_regex_falls_back_to_substring_and_notes_the_fallback(monkeypatch):
    opener = PodLogOpener(
        streams={
            ("app", False): _log_body(
                f"{_rfc3339(START)} literal [ token",
                f"{_rfc3339(START + timedelta(minutes=1))} ordinary",
            )
        }
    )
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(
            LOG_TOOL,
            _params(start_time=_rfc3339(START), end_time=_rfc3339(END), filter="["),
        )
    )
    assert outcome.status == "ok"
    content = json.dumps(_rows(outcome), ensure_ascii=False)
    assert "literal [ token" in content and "ordinary" not in content
    notes = json.dumps(_view(outcome), ensure_ascii=False).lower()
    assert "substring" in notes or "invalid regex" in notes or "fallback" in notes


def test_pathological_regex_is_hard_stopped_with_the_a2_timeout_class(monkeypatch):
    if shutil.which("jq") is None:
        pytest.skip("local jq is required for the A2 Kubernetes transport seam")
    opener = PodLogOpener(
        streams={("app", False): _log_body(f"{_rfc3339(START)} " + "a" * 20000 + "!")}
    )
    transport = kubernetes_core.KubernetesTransport(
        credentials={kubernetes_core.CREDENTIAL_REF: K8S_TOKEN},
        jq_path=shutil.which("jq"),
        opener=opener,
    )
    request = TransportRequest(
        operation_id="a3-timeout-t0",
        source="otel-demo",
        verb="logs",
        endpoint=K8S_API,
        selector={kubernetes_core.ENDPOINT_SELECTOR: K8S_API},
        params=_params(
            start_time=_rfc3339(START),
            end_time=_rfc3339(END),
            filter="(a+)+$",
        ),
        window=AUTHORIZED_WINDOW,
        timeout_seconds=0.25,
        max_result_bytes=4 * 1024 * 1024,
        credential_ref=kubernetes_core.CREDENTIAL_REF,
        tool=LOG_TOOL,
        target_id=TARGET_ID,
    )
    with pytest.raises(TransportTimeout) as raised:
        transport.fetch(request)
    assert raised.value.source_contact == "confirmed"


@pytest.mark.parametrize(
    "params",
    [
        _params(start_time="3600", end_time=_rfc3339(END)),
        _params(start_time=3600, end_time=_rfc3339(END)),
        _params(start_time=_rfc3339(START)),
        _params(end_time=_rfc3339(END)),
        _params(
            start_time=_rfc3339(AUTHORIZED_WINDOW.start - timedelta(seconds=1)),
            end_time=_rfc3339(END),
        ),
        _params(
            start_time=_rfc3339(START),
            end_time=_rfc3339(AUTHORIZED_WINDOW.end + timedelta(seconds=1)),
        ),
        _params(start_time="2026-09-14T10:00:00", end_time=_rfc3339(END)),
    ],
)
def test_time_inputs_are_absolute_bounded_and_echo_the_authorized_window(
    monkeypatch, params
):
    opener = PodLogOpener()
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(LOG_TOOL, params))
    assert outcome.status in {"error", "denied"}
    assert outcome.reason in {"INVALID_PARAMS", "WINDOW_OUT_OF_SCOPE"}
    assert _view(outcome)["content"] is None
    assert _view(outcome).get("window") == AUTHORIZED_WINDOW.as_json()
    assert opener.requests == []


def test_explicit_start_time_is_sent_as_since_time_and_ordinary_default_is_one_hour_before_frame_end(
    monkeypatch,
):
    opener = PodLogOpener()
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(LOG_TOOL, _params(start_time=_rfc3339(START), end_time=_rfc3339(END)))
    )
    assert outcome.status == "no_data"
    assert opener.query(1)["sinceTime"] == [_rfc3339(START)]

    opener = PodLogOpener()
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(LOG_TOOL, _params()))
    assert outcome.status == "no_data"
    assert opener.query(1)["sinceTime"] == [
        _rfc3339(AUTHORIZED_WINDOW.end - timedelta(hours=1))
    ]


def test_alert_default_window_starts_one_hour_before_alert_anchor(monkeypatch):
    opener = PodLogOpener()
    executor, alert, alert_window = _alert_executor(monkeypatch, opener)
    outcome = executor.execute(_request(_params(), window=alert_window))
    assert outcome.status == "no_data"
    assert opener.query(1)["sinceTime"] == [_rfc3339(alert.default_query_window[0])]
    assert opener.query(1)["sinceTime"] != [
        _rfc3339(alert_window.end - timedelta(hours=1))
    ]


@pytest.mark.parametrize("limit", [0, 1001, -1])
def test_limit_outside_one_to_1000_is_rejected_before_any_request(monkeypatch, limit):
    opener = PodLogOpener()
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(LOG_TOOL, _params(limit=limit)))
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert opener.requests == []


@pytest.mark.parametrize("limit", [1, 1000])
def test_limit_boundaries_one_and_1000_are_accepted(monkeypatch, limit):
    opener = PodLogOpener()
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(LOG_TOOL, _params(limit=limit)))
    assert outcome.status == "no_data"
    assert len(opener.requests) == 3  # Pod GET plus current and previous streams.


def test_default_limit_is_100_and_view_reports_displayed_and_omitted_lines(monkeypatch):
    lines = [f"{_rfc3339(START + timedelta(seconds=i))} line-{i}" for i in range(120)]
    opener = PodLogOpener(streams={("app", False): _log_body(*lines)})
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(LOG_TOOL, _params(start_time=_rfc3339(START), end_time=_rfc3339(END)))
    )
    assert outcome.status == "ok"
    view = _view(outcome)
    assert len(_rows(outcome)) == 100
    assert view["incomplete"] is True
    assert view.get("lines_shown") == 100
    assert view.get("lines_omitted") == 20
    assert view.get("row_unit") == "lines" or view.get("backend_lines_returned") == 120


def test_namespace_and_pod_names_are_validated_before_a_request(monkeypatch):
    opener = PodLogOpener()
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    for params in (_params(namespace="Bad_Namespace"), _params(pod_name="checkout_0")):
        outcome = executor.execute(_call(LOG_TOOL, params))
        assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert opener.requests == []


def test_container_name_is_url_encoded_and_no_otel_credential_is_used(monkeypatch):
    container = "sidecar/blue"
    opener = PodLogOpener(
        pod_body=_pod(containers=(container,)),
        streams={(container, False): _log_body(f"{_rfc3339(START)} encoded")},
    )
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(LOG_TOOL, _params(start_time=_rfc3339(START), end_time=_rfc3339(END)))
    )
    assert outcome.status == "ok"
    log_request = opener.requests[1]
    raw_query = urlsplit(_url(log_request)).query
    assert quote(container, safe="") in raw_query
    assert all(request.get_method() == "GET" for request in opener.requests)
    assert all(
        _headers(request).get("authorization") == f"Bearer {K8S_TOKEN}"
        for request in opener.requests
    )
    assert OTEL_TOKEN not in json.dumps(
        [_headers(request) for request in opener.requests]
    )
    assert kubernetes_core.CREDENTIAL_REF == "k8s-ro"
