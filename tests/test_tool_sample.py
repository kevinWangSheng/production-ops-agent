"""Tests for the direct tool sampling script (#580), no network or container.

Every item runs through the real ``otel-demo-ext`` executor factory; only the
``urllib.request.build_opener`` opener is replaced (the A2/A3/A6 contract
tests' seam) by one double that answers the Kubernetes API, Prometheus and
Jaeger by host. All credentials are low-entropy placeholders.
"""

from __future__ import annotations

import io
import json
import shutil
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts import tool_sample
from tests.m1_tool_support import FakeClock
from tests.test_m1_otel_demo_contract import NOW

K8S_API = "https://127.0.0.1:16443"
PROM_URL = "http://127.0.0.1:19090"
JAEGER_URL = "http://127.0.0.1:16686/jaeger/ui"
K8S_TOKEN = "k8s-aaaaaaaa357"
OTEL_TOKEN = "otel-aaaaaaaa357"
PROM_USERNAME = "prom-user-aaaa357"
PROM_PASSWORD = "prom-pass-aaaa357"
LANGSMITH_KEY = "ls-aaaaaaaa357"
TRACE_TAGS = "error=true"
NAMESPACE = "otel-demo"
POD = "checkout-0"
LOG_MARKER = "log-text-must-not-be-written"
WINDOW_END = NOW - timedelta(minutes=1)
WINDOW_START = WINDOW_END - timedelta(hours=1)


def _rfc3339(moment) -> str:
    return moment.isoformat().replace("+00:00", "Z")


class _Reply:
    def __init__(self, body: bytes):
        self._stream = io.BytesIO(body)
        self.status = 200

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _json(value) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def _pod_item(name: str, phase: str = "Running") -> dict:
    return {
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "creationTimestamp": "2026-09-14T00:10:00Z",
        },
        "spec": {"nodeName": "lab-worker", "containers": [{"image": "ghcr.io/c:v1"}]},
        "status": {"phase": phase},
    }


def _pod_list(names) -> bytes:
    return _json(
        {
            "apiVersion": "v1",
            "kind": "PodList",
            "metadata": {"resourceVersion": "1", "continue": ""},
            "items": [_pod_item(name) for name in names],
        }
    )


def _log_lines(count: int) -> bytes:
    lines = [
        f"{_rfc3339(WINDOW_START + timedelta(seconds=i))} {LOG_MARKER} {i}"
        for i in range(count)
    ]
    return ("\n".join(lines) + "\n").encode()


def _traces() -> bytes:
    start = int((WINDOW_START + timedelta(minutes=5)).timestamp() * 1_000_000)
    trace_id = "1" * 32
    return _json(
        {
            "data": [
                {
                    "traceID": trace_id,
                    "spans": [
                        {
                            "traceID": trace_id,
                            "spanID": "1" * 16,
                            "operationName": "GET /",
                            "startTime": start,
                            "duration": 25_000,
                            "tags": [{"key": "error", "type": "bool", "value": True}],
                            "logs": [],
                            "references": [],
                            "processID": "p1",
                        }
                    ],
                    "processes": {"p1": {"serviceName": "frontend", "tags": []}},
                }
            ],
            "total": 1,
            "errors": [],
        }
    )


def _instant() -> bytes:
    at = (WINDOW_END - timedelta(minutes=1)).timestamp()
    return _json(
        {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {"__name__": "up", "job": "x"}, "value": [at, "1"]}
                ],
            },
        }
    )


class LabOpener:
    """One urllib double for the three lab sources, routed by port and path."""

    def __init__(self, *, log_lines: int = 150, pods=("checkout-0", "cart-0")):
        self.log_lines = log_lines
        self.pods = pods
        self.requests: list[str] = []

    def open(self, request, timeout=None):
        url = (
            request.full_url
            if isinstance(request, urllib.request.Request)
            else str(request)
        )
        self.requests.append(url)
        parts = urlsplit(url)
        path = parts.path
        if parts.port == 16443:
            if path.endswith("/log"):
                previous = parse_qs(parts.query).get("previous", ["false"])[0]
                if previous.lower() == "true":
                    return _Reply(b"")
                return _Reply(_log_lines(self.log_lines))
            if path.endswith(f"/pods/{POD}"):
                return _Reply(
                    _json(
                        {
                            "apiVersion": "v1",
                            "kind": "Pod",
                            "metadata": {"name": POD, "namespace": NAMESPACE},
                            "spec": {"containers": [{"name": "app"}]},
                            "status": {"phase": "Running"},
                        }
                    )
                )
            if path.endswith("/pods"):
                return _Reply(_pod_list(self.pods))
        if parts.port == 19090 and path.endswith("/api/v1/query"):
            return _Reply(_instant())
        if parts.port == 16686 and path.endswith("/api/traces"):
            return _Reply(_traces())
        raise urllib.error.URLError(f"unexpected request {url}")


@pytest.fixture
def env(tmp_path):
    if shutil.which("jq") is None:
        pytest.skip("local jq is required for the otel-demo-ext worker seam")
    ca_file = tmp_path / "synthetic-ca.pem"
    ca_file.write_text("synthetic ca placeholder\n")
    return {
        "OPSPILOT_K8S_API_URL": K8S_API,
        "OPSPILOT_K8S_TOKEN": K8S_TOKEN,
        "OPSPILOT_K8S_CA_FILE": str(ca_file),
        "OPSPILOT_K8S_NAMESPACE": NAMESPACE,
        "OPSPILOT_OTEL_PROMETHEUS_URL": PROM_URL,
        "OPSPILOT_OTEL_JAEGER_URL": JAEGER_URL,
        "OPSPILOT_OTEL_TOKEN": OTEL_TOKEN,
        "OPSPILOT_OTEL_PROMETHEUS_USERNAME": PROM_USERNAME,
        "OPSPILOT_OTEL_PROMETHEUS_PASSWORD": PROM_PASSWORD,
        # The full lab gate (check_lab_target), as on the lab worker.
        "OPSPILOT_TRACE": "lab",
        "LANGSMITH_PROJECT": "opspilot-lab-tool-sample",
        "LANGSMITH_API_KEY": LANGSMITH_KEY,
    }


def _install(monkeypatch, opener: LabOpener) -> LabOpener:
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: opener)
    return opener


def _main(env, evidence: Path, *extra: str) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = tool_sample.main(
        [
            "--evidence-dir",
            str(evidence),
            "--window-start",
            WINDOW_START.isoformat(),
            "--window-end",
            WINDOW_END.isoformat(),
            "--log-pod",
            POD,
            "--trace-tags",
            TRACE_TAGS,
            *extra,
        ],
        env=env,
        clock=FakeClock(start=NOW),
        out=lines.append,
    )
    return code, lines


def _load(evidence: Path, name: str) -> dict:
    return json.loads((evidence / name).read_text())


def test_all_items_pass_through_the_product_executor(env, monkeypatch, tmp_path):
    opener = _install(monkeypatch, LabOpener())
    evidence = tmp_path / "evidence"
    items = "tabular,logs,secret,traces,prom_instant"
    code, lines = _main(env, evidence, "--items", items)
    assert code == 0, lines
    result = _load(evidence, "result.json")
    assert result["all_executed_passed"] is True
    assert result["all_requested_passed"] is True
    assert {
        name: item["verdict"] for name, item in result["items"].items()
    } == dict.fromkeys(items.split(","), "pass")
    assert result["credential_hits"] == 0
    assert result["versions"]["tool_schema_revision"].startswith("otel-demo-ext-")

    tabular = _load(evidence, "tabular.json")
    assert tabular["model_view"]["header"] == ["NAME", "STATUS", "NODE", "IMAGE"]
    assert tabular["model_view"]["count"] == 2
    assert tabular["content_summary"]["length"] == 2
    assert tabular["evidence"]["raw_sha256"]

    logs = _load(evidence, "logs.json")
    assert logs["model_view"]["incomplete"] is True
    assert logs["model_view"]["lines_shown"] == 100
    assert logs["model_view"]["lines_omitted"] == 50
    assert set(logs["content_summary"]["first_rows"][0]) == {
        "timestamp",
        "container",
        "previous",
    }

    secret = _load(evidence, "secret.json")
    assert (secret["reason"], secret["source_contact"]) == (
        "RESOURCE_NOT_ALLOWED",
        "none",
    )
    assert [item["seconds"] for item in secret["ledger_charges"]] == [0]
    assert not any("/secrets" in url for url in opener.requests)

    for name in ("traces", "prom_instant"):
        record = _load(evidence, f"{name}.json")
        assert record["status"] == "ok" and record["model_view"]["result_count"] >= 1

    written = "".join(path.read_text() for path in evidence.iterdir())
    assert LOG_MARKER not in written
    for value in (K8S_TOKEN, OTEL_TOKEN, PROM_USERNAME, PROM_PASSWORD, LANGSMITH_KEY):
        assert value not in written
    traces = _load(evidence, "traces.json")
    assert traces["model_view"]["query"]["tags"] == TRACE_TAGS
    search = next(
        url for url in opener.requests if url.split("?")[0].endswith("/api/traces")
    )
    assert "tags=" in search
    assert secret["dispatched"] is False
    run_md = (evidence / "run.md").read_text()
    assert "| logs | `fetch_pod_logs` | ok / None | pass |" in run_md


def test_logs_under_the_view_limit_fail_the_incompleteness_verdict(
    env, monkeypatch, tmp_path
):
    _install(monkeypatch, LabOpener(log_lines=40))
    evidence = tmp_path / "evidence"
    code, _ = _main(env, evidence, "--items", "logs")
    assert code == 1
    logs = _load(evidence, "logs.json")
    assert logs["verdict"] == "fail"
    failed = {check["check"] for check in logs["checks"] if not check["ok"]}
    assert {"incomplete is True", "lines_omitted > 0"} <= failed


def test_tabular_header_must_match_requested_columns(env, monkeypatch, tmp_path):
    _install(monkeypatch, LabOpener())
    evidence = tmp_path / "evidence"
    code, _ = _main(
        env,
        evidence,
        "--items",
        "tabular",
        "--tabular-columns",
        "NAME:.metadata.name,PHASE:.status.phase",
    )
    assert code == 0
    assert _load(evidence, "tabular.json")["model_view"]["header"] == ["NAME", "PHASE"]


def test_set_overrides_one_parameter(env, monkeypatch, tmp_path):
    _install(monkeypatch, LabOpener(log_lines=150))
    evidence = tmp_path / "evidence"
    code, _ = _main(env, evidence, "--items", "logs", "--set", "logs.limit=20")
    assert code == 0
    logs = _load(evidence, "logs.json")
    assert logs["params"]["limit"] == 20
    assert logs["model_view"]["lines_shown"] == 20


def test_unregistered_tool_is_skipped_not_called(env, monkeypatch, tmp_path):
    opener = _install(monkeypatch, LabOpener())
    profile = tool_sample.select_profile({tool_sample.PROFILE_ENV: tool_sample.PROFILE})
    names = {
        item["function"]["name"]
        for item in profile.face(FakeClock(start=NOW)).tool_schemas
    }
    if tool_sample.ITEMS["rules"] in names:
        pytest.skip("list_prometheus_rules is registered in this build")
    evidence = tmp_path / "evidence"
    code, _ = _main(env, evidence, "--items", "rules")
    assert code == 1  # nothing executed is not a pass
    assert _load(evidence, "rules.json")["verdict"] == "skipped"
    assert opener.requests == []


def test_a_credential_in_the_output_writes_nothing(env, monkeypatch, tmp_path):
    # A Pod named like the token puts it in the tabular preview rows.
    _install(monkeypatch, LabOpener(pods=(K8S_TOKEN,)))
    evidence = tmp_path / "evidence"
    code, lines = _main(env, evidence, "--items", "tabular")
    assert code == 2
    assert any("CREDENTIAL_IN_OUTPUT" in line for line in lines)
    assert not evidence.exists() or not any(evidence.iterdir())


@pytest.mark.parametrize(
    "overrides, code",
    [
        (
            {"OPSPILOT_K8S_API_URL": "https://k8s.example.com:6443"},
            "KUBERNETES_NOT_LAB",
        ),
        (
            {"OPSPILOT_OTEL_PROMETHEUS_URL": "http://prom.example.com"},
            "PROMETHEUS_NOT_LAB",
        ),
    ],
)
def test_non_lab_sources_are_refused_before_any_request(
    env, monkeypatch, tmp_path, overrides, code
):
    opener = _install(monkeypatch, LabOpener())
    exit_code, lines = _main({**env, **overrides}, tmp_path / "evidence")
    assert exit_code == 2
    assert any(code in line for line in lines)
    assert opener.requests == []


def test_window_outside_the_24h_frame_is_refused(env, monkeypatch, tmp_path):
    opener = _install(monkeypatch, LabOpener())
    lines: list[str] = []
    code = tool_sample.main(
        [
            "--evidence-dir",
            str(tmp_path / "evidence"),
            "--window-start",
            (NOW - timedelta(hours=30)).isoformat(),
            "--window-end",
            WINDOW_END.isoformat(),
            "--items",
            "tabular",
        ],
        env=env,
        clock=FakeClock(start=NOW),
        out=lines.append,
    )
    assert code == 2
    assert any("WINDOW_OUTSIDE_FRAME" in line for line in lines)
    assert opener.requests == []


def test_missing_log_pod_and_existing_evidence_are_refused(env, monkeypatch, tmp_path):
    _install(monkeypatch, LabOpener())
    lines: list[str] = []
    code = tool_sample.main(
        ["--evidence-dir", str(tmp_path / "e"), "--items", "logs"],
        env=env,
        clock=FakeClock(start=NOW),
        out=lines.append,
    )
    assert code == 2 and any("LOG_POD_REQUIRED" in line for line in lines)

    evidence = tmp_path / "evidence"
    assert _main(env, evidence, "--items", "secret")[0] == 0
    code, lines = _main(env, evidence, "--items", "secret")
    assert code == 2 and any("EVIDENCE_EXISTS" in line for line in lines)


# -- review fixes (PR #607) ---------------------------------------------------


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({"OPSPILOT_TRACE": "off"}, "TRACE_MODE_NOT_LAB"),
        ({"LANGSMITH_PROJECT": "production"}, "PROJECT_NOT_LAB"),
        ({"LANGSMITH_API_KEY": ""}, "LANGSMITH_KEY_MISSING"),
    ],
)
def test_the_whole_lab_gate_is_fail_closed(env, monkeypatch, tmp_path, overrides, code):
    opener = _install(monkeypatch, LabOpener())
    exit_code, lines = _main({**env, **overrides}, tmp_path / "evidence")
    assert exit_code == 2
    assert any(code in line for line in lines)
    assert opener.requests == []
    assert not (tmp_path / "evidence").exists()


@pytest.mark.parametrize("value", [PROM_USERNAME, PROM_PASSWORD, LANGSMITH_KEY])
def test_every_product_credential_in_the_output_writes_nothing(
    env, monkeypatch, tmp_path, value
):
    # A Pod name carrying the value puts it in the tabular preview rows.
    _install(monkeypatch, LabOpener(pods=(value,)))
    evidence = tmp_path / "evidence"
    code, lines = _main(env, evidence, "--items", "tabular")
    assert code == 2
    assert any("CREDENTIAL_IN_OUTPUT" in line for line in lines)
    assert not evidence.exists() or not any(evidence.iterdir())


@pytest.mark.parametrize(
    "extra",
    [
        ("--items", "tabular", "--set", f"tabular.{K8S_TOKEN}"),
        ("--items", "tabular", "--set", f"{K8S_TOKEN}"),
        ("--items", f"tabular,{OTEL_TOKEN}"),
        ("--items", "logs", "--log-limit", PROM_PASSWORD),
        ("--items", "tabular", f"--{LANGSMITH_KEY}"),
        ("--items", "tabular", "--window-end", PROM_USERNAME),
        ("--items", "tabular", "--allow-skip", K8S_TOKEN),
    ],
)
def test_error_messages_never_echo_a_credential(env, monkeypatch, tmp_path, extra):
    _install(monkeypatch, LabOpener())
    code, lines = _main(env, tmp_path / "evidence", *extra)
    assert code == 2, lines
    printed = "\n".join(lines)
    for value in (K8S_TOKEN, OTEL_TOKEN, PROM_USERNAME, PROM_PASSWORD, LANGSMITH_KEY):
        assert value not in printed


def _rules_registered() -> bool:
    profile = tool_sample.select_profile({tool_sample.PROFILE_ENV: tool_sample.PROFILE})
    names = {
        item["function"]["name"]
        for item in profile.face(FakeClock(start=NOW)).tool_schemas
    }
    return tool_sample.ITEMS["rules"] in names


def test_a_requested_skip_fails_unless_allowed(env, monkeypatch, tmp_path):
    if _rules_registered():
        pytest.skip("list_prometheus_rules is registered in this build")
    _install(monkeypatch, LabOpener())
    evidence = tmp_path / "denied"
    code, _ = _main(env, evidence, "--items", "secret,rules")
    assert code == 1
    result = _load(evidence, "result.json")
    assert result["all_executed_passed"] is True
    assert result["all_requested_passed"] is False
    assert result["items"]["rules"]["skip_allowed"] is False

    evidence = tmp_path / "allowed"
    code, _ = _main(env, evidence, "--items", "secret,rules", "--allow-skip", "rules")
    assert code == 0
    result = _load(evidence, "result.json")
    assert result["all_requested_passed"] is True
    assert result["allowed_skips"] == ["rules"]
    assert "skipped (allowed)" in (evidence / "run.md").read_text()


def test_traces_without_a_tag_condition_are_refused(env, monkeypatch, tmp_path):
    opener = _install(monkeypatch, LabOpener())
    lines: list[str] = []
    code = tool_sample.main(
        [
            "--evidence-dir",
            str(tmp_path / "evidence"),
            "--window-start",
            WINDOW_START.isoformat(),
            "--window-end",
            WINDOW_END.isoformat(),
            "--items",
            "traces",
        ],
        env=env,
        clock=FakeClock(start=NOW),
        out=lines.append,
    )
    assert code == 2
    assert any("TRACE_TAGS_REQUIRED" in line for line in lines)
    assert opener.requests == []


# Verdict arithmetic on hand-built outcomes: the shapes the executor cannot be
# made to produce through a source double.


class _Operation:
    def __init__(self, dispatched: bool):
        self.dispatched = dispatched


class _Outcome:
    def __init__(
        self, view, *, status="ok", reason=None, contact="confirmed", dispatched=True
    ):
        self.model_view = view
        self.status = status
        self.reason = reason
        self.source_contact = contact
        self.operation = _Operation(dispatched)
        self.evidence = None
        self.adopted = True


def _failed(item, outcome, *, params=None, charges=()):
    checks = tool_sample.judge(
        item, outcome, params=params or {}, charges=list(charges)
    )
    return {check["check"] for check in checks if not check["ok"]}


def test_log_omitted_count_must_equal_returned_minus_shown():
    rows = [{"timestamp": "t", "container": "app", "previous": False}] * 100
    view = {
        "content": rows,
        "incomplete": True,
        "lines_shown": 100,
        "backend_lines_returned": 150,
        "lines_omitted": 50,
    }
    assert _failed("logs", _Outcome(view)) == set()
    assert _failed("logs", _Outcome({**view, "lines_omitted": 7})) == {
        "lines_omitted == backend_lines_returned - lines_shown"
    }


def test_secret_needs_a_settled_reservation_and_an_undispatched_operation():
    refused = {"content": None}
    settled = [{"settled": True, "seconds": 0.0}]

    def secret(**kwargs):
        return _Outcome(
            refused,
            status="error",
            reason="RESOURCE_NOT_ALLOWED",
            contact="none",
            dispatched=kwargs.get("dispatched", False),
        )

    assert _failed("secret", secret(), charges=settled) == set()
    assert _failed("secret", secret(), charges=()) == {
        "reserved and every reservation settled at 0 s"
    }
    assert _failed("secret", secret(dispatched=True), charges=settled) == {
        "operation.dispatched is False"
    }


def test_traces_verdict_checks_the_recorded_tag_condition():
    view = {"content": [], "result_count": 1, "query": {"service": "frontend"}}
    params = {"service": "frontend", "tags": TRACE_TAGS}
    assert _failed("traces", _Outcome(view), params=params) == {
        "request carried the tag condition"
    }
    tagged = {**view, "query": {"service": "frontend", "tags": TRACE_TAGS}}
    assert _failed("traces", _Outcome(tagged), params=params) == set()
