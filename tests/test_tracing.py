"""``opspilot.tracing``: off is nothing, lab is fail-closed, spans are allowlisted.

Every exporting test uses the SDK's in-memory exporter; no test opens a
socket to LangSmith. Secret values below are synthetic markers chosen to be
unlike any real credential; no test reads a real ``.env``.
"""

import json
import logging
import time
from uuid import uuid4

import pytest
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from opspilot import tracing
from opspilot.investigation.client import DeepSeekClient
from opspilot.investigation.loop import ModelCall, ModelError
from opspilot.tools import TransportResponse
from opspilot.tools.outcomes import PROJECTION_REVISION
from opspilot.tracing import NullTracer, Tracer, check_lab_target
from tests.m1_tool_support import WINDOW_START, body, build, request

FAKE_LANGSMITH_KEY = "fake-langsmith-key-not-real-0000"
FAKE_DEEPSEEK_KEY = "fake-deepseek-key-not-real-1111"
FAKE_REASONING = "fake-reasoning-content-must-not-leave-provider-channel"
FAKE_PRIOR_REASONING = "fake-prior-turn-reasoning-replayed-to-deepseek"
SECRETS = (FAKE_LANGSMITH_KEY, FAKE_DEEPSEEK_KEY, FAKE_REASONING, FAKE_PRIOR_REASONING)

VERSIONS = {
    "prompt_revision": "prompt-rev-test",
    "tool_schema_revision": "tool-rev-test",
    "context_policy_revision": "ctx-rev-test",
    "unlisted_version": "must-not-appear",
}


def lab_env(**overrides):
    env = {
        "OPSPILOT_TRACE": "lab",
        "LANGSMITH_API_KEY": FAKE_LANGSMITH_KEY,
        "LANGSMITH_PROJECT": "opspilot-lab-test",
        "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
        "OPSPILOT_TOOL_PROFILE": "fixture",
    }
    env.update(overrides)
    return env


@pytest.fixture(autouse=True)
def _reset_tracer():
    yield
    tracing.reset()


class _FixedResponse:
    def __init__(self, payload: bytes) -> None:
        self._chunks = [payload, b""]
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self, size=-1):
        return self._chunks.pop(0) if self._chunks else b""


class _Opener:
    """Records the request headers, like a leak would need to."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.headers = {}

    def open(self, request, timeout):
        self.headers = dict(request.headers)
        return _FixedResponse(self._payload)


def reply_payload(*, tool_call=True):
    message = {
        "role": "assistant",
        "content": None if tool_call else '{"report": "done"}',
        "reasoning_content": FAKE_REASONING,
    }
    if tool_call:
        message["tool_calls"] = [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "metrics.range_query",
                    "arguments": '{"expr": "rate(http_errors[5m])"}',
                },
            }
        ]
    return json.dumps(
        {
            "id": "resp-1",
            "model": "deepseek-flash",
            "choices": [{"message": message, "finish_reason": "tool_calls"}],
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 34,
                "total_tokens": 154,
                "prompt_cache_hit_tokens": 7,
                "provider_private_counter": 99,
            },
        }
    ).encode()


def model_call(**overrides):
    fields = {
        "messages": (
            {"role": "system", "content": "You are OpsPilot."},
            {"role": "user", "content": "Why are checkout errors elevated?"},
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": FAKE_PRIOR_REASONING,
                "tool_calls": [
                    {
                        "id": "call-0",
                        "type": "function",
                        "function": {"name": "metrics.range_query", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-0", "content": '{"rows": []}'},
        ),
        "tools": ({"type": "function", "function": {"name": "metrics.range_query"}},),
        "json_mode": False,
        "max_tokens": 2048,
        "timeout_seconds": 30.0,
    }
    fields.update(overrides)
    return ModelCall(**fields)


def executor_with_rows():
    executor, transport, _sink, _clock = build()
    transport.response = TransportResponse(
        body=body([{"metric": "http_errors_rate", "value": 0.042}]),
        data_as_of=WINDOW_START,
    )
    return executor


def one_lab_run(exporter, *, env=None):
    """A Run span with one model call and one tool call, exported to ``exporter``."""
    tracer = tracing.configure(env or lab_env(), exporter=exporter)
    opener = _Opener(reply_payload())
    client = DeepSeekClient(FAKE_DEEPSEEK_KEY, opener=opener)
    incident, run = uuid4(), uuid4()
    with tracer.run(incident_id=incident, run_id=run, versions=VERSIONS) as span:
        reply = client.complete(model_call())
        outcome = executor_with_rows().execute(request())
        span.settle("published", None)
    assert reply.reasoning_content == FAKE_REASONING  # the business path keeps it
    assert outcome.status == "ok"
    assert opener.headers["Authorization"] == "Bearer " + FAKE_DEEPSEEK_KEY
    tracer.shutdown()
    return tracer, incident, run


def by_name(spans):
    return {span.name: span for span in spans}


def all_attribute_text(spans):
    parts = []
    for span in spans:
        parts.append(json.dumps(dict(span.attributes), default=str))
        parts.append(json.dumps(dict(span.resource.attributes), default=str))
        parts.append(span.name)
        for event in span.events:
            parts.append(json.dumps(dict(event.attributes), default=str))
    return "\n".join(parts)


# -- off -----------------------------------------------------------------------


def test_default_is_the_null_tracer_and_spans_are_no_ops():
    tracer = tracing.configure({})
    assert isinstance(tracer, NullTracer)
    assert tracing.tracer() is tracer
    with tracer.run(incident_id="i", run_id="r", versions={}) as span:
        assert span.trace_id is None
        with tracer.model_call(model_call()) as inner:
            inner.reply(None)  # never inspected: nothing is built
    assert tracer.last_trace_id is None


def test_unknown_mode_stays_off(caplog):
    with caplog.at_level(logging.ERROR, logger="opspilot.tracing"):
        tracer = tracing.configure({"OPSPILOT_TRACE": "prod"})
    assert isinstance(tracer, NullTracer)
    assert "not a mode" in caplog.text


def test_off_mode_client_and_executor_run_unchanged():
    tracing.configure({"OPSPILOT_TRACE": "off"})
    client = DeepSeekClient(FAKE_DEEPSEEK_KEY, opener=_Opener(reply_payload()))
    assert client.complete(model_call()).finish_reason == "tool_calls"
    assert executor_with_rows().execute(request()).status == "ok"


# -- fail-closed ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"OPSPILOT_TOOL_PROFILE": "production"}, "TOOL_PROFILE_NOT_LAB"),
        (
            {"OPSPILOT_OTEL_PROMETHEUS_URL": "http://prom.example.com"},
            "PROMETHEUS_NOT_LAB",
        ),
        ({"OPSPILOT_OTEL_JAEGER_URL": "http://10.0.0.5:16686"}, "JAEGER_NOT_LAB"),
        ({"LANGSMITH_PROJECT": "opspilot-m0"}, "PROJECT_NOT_LAB"),
        ({"LANGSMITH_PROJECT": ""}, "PROJECT_NOT_LAB"),
        ({"LANGSMITH_API_KEY": ""}, "LANGSMITH_KEY_MISSING"),
        (
            {"LANGSMITH_ENDPOINT": "https://smith.example.com"},
            "LANGSMITH_ENDPOINT_NOT_ALLOWED",
        ),
        (
            {"LANGSMITH_ENDPOINT": "http://api.smith.langchain.com"},
            "LANGSMITH_ENDPOINT_NOT_ALLOWED",
        ),
    ],
)
def test_each_lab_precondition_is_checked(override, code):
    assert check_lab_target(lab_env()) == ()
    assert code in check_lab_target(lab_env(**override))


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:19090",
        "http://localhost:16686/jaeger/ui",
        "http://prometheus.monitoring.svc:9090",
        "http://jaeger-query.observability.svc.cluster.local:16686",
    ],
)
def test_loopback_and_kind_in_cluster_endpoints_are_lab(url):
    env = lab_env(OPSPILOT_OTEL_PROMETHEUS_URL=url, OPSPILOT_OTEL_JAEGER_URL=url)
    assert check_lab_target(env) == ()


def test_env_var_set_but_target_not_lab_exports_nothing_at_startup(caplog):
    exporter = InMemorySpanExporter()
    with caplog.at_level(logging.ERROR, logger="opspilot.tracing"):
        tracer = tracing.configure(
            lab_env(LANGSMITH_PROJECT="opspilot-m0"), exporter=exporter
        )
    assert isinstance(tracer, NullTracer)
    with tracer.run(incident_id="i", run_id="r", versions={}):
        DeepSeekClient(FAKE_DEEPSEEK_KEY, opener=_Opener(reply_payload())).complete(
            model_call()
        )
    tracing.shutdown()
    assert exporter.get_finished_spans() == ()
    assert "PROJECT_NOT_LAB" in caplog.text


def test_a_run_whose_target_stopped_being_lab_exports_nothing(caplog):
    """The per-Run check reads the live environment, not the startup one."""
    env = lab_env()
    exporter = InMemorySpanExporter()
    tracer = tracing.configure(env, exporter=exporter)
    assert isinstance(tracer, Tracer)
    env["OPSPILOT_TOOL_PROFILE"] = "production"
    with caplog.at_level(logging.WARNING, logger="opspilot.tracing"):
        with tracer.run(incident_id="i", run_id="r", versions=VERSIONS) as span:
            assert span.trace_id is None
            DeepSeekClient(FAKE_DEEPSEEK_KEY, opener=_Opener(reply_payload())).complete(
                model_call()
            )
            executor_with_rows().execute(request())
    tracer.shutdown()
    assert exporter.get_finished_spans() == ()
    assert "TOOL_PROFILE_NOT_LAB" in caplog.text


def test_model_and_tool_spans_outside_a_run_are_not_exported():
    exporter = InMemorySpanExporter()
    tracer = tracing.configure(lab_env(), exporter=exporter)
    DeepSeekClient(FAKE_DEEPSEEK_KEY, opener=_Opener(reply_payload())).complete(
        model_call()
    )
    executor_with_rows().execute(request())
    tracer.shutdown()
    assert exporter.get_finished_spans() == ()


# -- span tree and attributes --------------------------------------------------


def test_lab_run_exports_run_llm_tool_tree_with_allowlisted_attributes():
    exporter = InMemorySpanExporter()
    tracer, incident, run = one_lab_run(exporter)
    spans = by_name(exporter.get_finished_spans())
    assert set(spans) == {
        "opspilot.run",
        "deepseek.chat.completions",
        "tool:metrics.range_query",
    }
    root, llm, tool = (
        spans["opspilot.run"],
        spans["deepseek.chat.completions"],
        spans["tool:metrics.range_query"],
    )
    assert root.parent is None
    assert llm.parent.span_id == root.context.span_id
    assert tool.parent.span_id == root.context.span_id
    assert {s.context.trace_id for s in spans.values()} == {root.context.trace_id}
    assert tracer.last_trace_id == format(root.context.trace_id, "032x")

    r = dict(root.attributes)
    assert r["langsmith.span.kind"] == "chain"
    assert r["langsmith.metadata.incident_id"] == str(incident)
    assert r["langsmith.metadata.run_id"] == str(run)
    assert r["langsmith.metadata.prompt_revision"] == "prompt-rev-test"
    assert r["langsmith.metadata.tool_schema_revision"] == "tool-rev-test"
    assert r["langsmith.metadata.projection_revision"] == PROJECTION_REVISION
    assert "langsmith.metadata.unlisted_version" not in r
    assert r["opspilot.run.status"] == "published"
    assert r["opspilot.run.model_calls"] == 1
    assert r["opspilot.run.tool_calls"] == 1

    m = dict(llm.attributes)
    assert m["langsmith.span.kind"] == "llm"
    assert m["langsmith.metadata.model_call_seq"] == 1
    assert m["gen_ai.request.model"] == "deepseek-flash"
    assert m["gen_ai.response.model"] == "deepseek-flash"
    assert m["gen_ai.usage.input_tokens"] == 120
    assert m["gen_ai.usage.output_tokens"] == 34
    assert m["gen_ai.usage.total_tokens"] == 154
    assert tuple(m["gen_ai.response.finish_reasons"]) == ("tool_calls",)
    assert m["opspilot.model.tool_call_count"] == 1
    assert m["gen_ai.prompt.0.role"] == "system"
    assert m["gen_ai.prompt.1.content"] == "Why are checkout errors elevated?"
    assert m["gen_ai.prompt.2.role"] == "assistant"
    assert m["gen_ai.prompt.3.role"] == "tool"
    assert m["gen_ai.completion.0.role"] == "assistant"
    assert "metrics.range_query" in m["gen_ai.completion.0.content"]
    assert not any(key.startswith("gen_ai.usage.") and "cache" in key for key in m)
    assert llm.end_time > llm.start_time

    t = dict(tool.attributes)
    assert t["langsmith.span.kind"] == "tool"
    assert t["langsmith.metadata.tool_call_seq"] == 1
    assert t["gen_ai.tool.name"] == "metrics.range_query"
    assert t["opspilot.tool.name"] == "metrics.range_query"
    assert t["opspilot.tool.target_id"] == "checkout-prod"
    assert t["opspilot.tool.status"] == "ok"
    assert t["opspilot.tool.source_contact"] == "confirmed"
    assert t["opspilot.tool.raw_bytes"] > 0
    assert t["opspilot.tool.view_bytes"] > 0
    assert t["opspilot.tool.evidence_id"]
    assert "http_errors_rate" in t["gen_ai.completion"]
    assert "credential_ref" not in json.dumps(t)
    assert "prom-ro-checkout" not in json.dumps(t)


def test_a_model_error_is_recorded_by_code_only():
    exporter = InMemorySpanExporter()
    tracer = tracing.configure(lab_env(), exporter=exporter)

    class _Refusing:
        def open(self, request, timeout):
            raise OSError(
                "connection refused with secret-looking text " + FAKE_DEEPSEEK_KEY
            )

    client = DeepSeekClient(FAKE_DEEPSEEK_KEY, opener=_Refusing())
    with tracer.run(incident_id="i", run_id="r", versions={}) as span:
        with pytest.raises(ModelError):
            client.complete(model_call())
        span.settle("handed_off", "MODEL_UNAVAILABLE")
    tracer.shutdown()
    spans = by_name(exporter.get_finished_spans())
    llm = dict(spans["deepseek.chat.completions"].attributes)
    assert llm["error.type"] == "MODEL_UNAVAILABLE"
    assert spans["deepseek.chat.completions"].status.is_ok is False
    assert FAKE_DEEPSEEK_KEY not in all_attribute_text(exporter.get_finished_spans())


def test_a_denied_tool_call_still_gets_a_span():
    exporter = InMemorySpanExporter()
    tracer = tracing.configure(lab_env(), exporter=exporter)
    with tracer.run(incident_id="i", run_id="r", versions={}):
        outcome = executor_with_rows().execute(request(tool_name="not.registered"))
    tracer.shutdown()
    assert outcome.status == "denied"
    # Unregistered names never reach the span (review round 2, #2).
    tool = by_name(exporter.get_finished_spans())["tool:unknown"]
    assert dict(tool.attributes)["opspilot.tool.reason"] == "TOOL_NOT_REGISTERED"


def test_long_attribute_values_are_truncated():
    exporter = InMemorySpanExporter()
    tracer = tracing.configure(lab_env(), exporter=exporter)
    huge = "x" * 100_000
    call = model_call(messages=({"role": "user", "content": huge},))
    with tracer.run(incident_id="i", run_id="r", versions={}):
        DeepSeekClient(FAKE_DEEPSEEK_KEY, opener=_Opener(reply_payload())).complete(
            call
        )
    tracer.shutdown()
    llm = by_name(exporter.get_finished_spans())["deepseek.chat.completions"]
    content = dict(llm.attributes)["gen_ai.prompt.0.content"]
    assert len(content) == tracing._MAX_ATTR_CHARS
    assert content.endswith("[truncated]")


# -- data flow contract --------------------------------------------------------


def test_no_secret_or_private_protocol_field_reaches_any_span():
    exporter = InMemorySpanExporter()
    one_lab_run(exporter)
    text = all_attribute_text(exporter.get_finished_spans())
    for secret in SECRETS:
        assert secret not in text
    assert "reasoning_content" not in text
    assert "Authorization" not in text
    assert "Bearer" not in text
    assert "x-api-key" not in text
    assert "provider_private_counter" not in text
    assert "prompt_cache_hit_tokens" not in text


def test_attribute_constructors_refuse_raw_dicts():
    with pytest.raises(TypeError):
        tracing._model_call_attributes({"messages": []}, seq=1)
    with pytest.raises(TypeError):
        tracing._model_reply_attributes({"content": "x", "reasoning_content": "y"})
    with pytest.raises(TypeError):
        tracing._tool_request_attributes(
            {"tool_name": "x"}, seq=1, tool_name=None, target_id=None
        )
    with pytest.raises(TypeError):
        tracing._tool_outcome_attributes({"status": "ok"})


def test_the_only_span_attribute_writers_live_in_tracing():
    """Structural: ``set_attribute`` is called in ``opspilot/tracing.py`` only."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "opspilot"
    offenders = [
        path.relative_to(root.parent).as_posix()
        for path in root.rglob("*.py")
        if path.name != "tracing.py" and "set_attribute" in path.read_text()
    ]
    assert offenders == []


# -- export failure is bounded loss ------------------------------------------


def test_unreachable_endpoint_does_not_affect_the_run(caplog):
    exporter = OTLPSpanExporter(endpoint="http://127.0.0.1:9/v1/traces", timeout=1)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="opspilot.tracing"):
        tracer, _, _ = one_lab_run(exporter)
    assert time.monotonic() - started < 30
    assert isinstance(tracer, Tracer)
    assert tracer.dropped_spans == 3
    assert "dropped spans" in caplog.text


# -- independent review of PR #108 (2026-10-05): one regression per finding ----


@pytest.mark.parametrize(
    "url",
    [
        "http://127.attacker.com:9090",
        "http://127.0.0.1.attacker.example:9090",
        "http://user@127.0.0.1:9090",
        "http://user:pw@localhost:9090",
        "http://localhost.attacker.example:9090",
        "http://evil.svc.attacker.example:9090",
        "http://prometheus.monitoring.svc.cluster.local.attacker.example:9090",
        "http://.svc:9090",
        "http://10.0.0.5:9090",
        "http://[::2]:9090",
        "not a url",
    ],
)
def test_finding_1_lab_host_rejects_lookalikes_and_userinfo(url):
    assert tracing._is_lab_host("") is False
    assert tracing._is_lab_host(url) is False
    env = lab_env(OPSPILOT_OTEL_PROMETHEUS_URL=url)
    assert "PROMETHEUS_NOT_LAB" in check_lab_target(env)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:19090",
        "http://127.255.0.9:19090",
        "http://[::1]:16686",
        "http://LOCALHOST:16686",
        "http://localhost.:16686",
        "http://prometheus.monitoring.svc:9090",
        "http://Jaeger-Query.Observability.svc.cluster.local.:16686",
    ],
)
def test_finding_1_real_loopback_and_cluster_hosts_are_lab(url):
    assert tracing._is_lab_host(url) is True


def test_finding_2_per_run_check_reverifies_the_mode():
    """configure in lab, env flips to off, a new Run must export nothing."""
    env = lab_env()
    exporter = InMemorySpanExporter()
    tracer = tracing.configure(env, exporter=exporter)
    assert isinstance(tracer, Tracer)
    env["OPSPILOT_TRACE"] = "off"
    with tracer.run(incident_id="i", run_id="r", versions=VERSIONS) as span:
        assert span.trace_id is None
        DeepSeekClient(FAKE_DEEPSEEK_KEY, opener=_Opener(reply_payload())).complete(
            model_call()
        )
    tracer.shutdown()
    assert exporter.get_finished_spans() == ()
    assert "TRACE_MODE_NOT_LAB" in check_lab_target(env)


def test_finding_3_a_refused_nested_run_detaches_its_children():
    """The inner Run's model/tool spans must not join the outer trace."""
    env = lab_env()
    exporter = InMemorySpanExporter()
    tracer = tracing.configure(env, exporter=exporter)
    with tracer.run(incident_id="outer", run_id="outer", versions=VERSIONS):
        env["LANGSMITH_PROJECT"] = "opspilot-m0"
        with tracer.run(incident_id="inner", run_id="inner", versions=VERSIONS):
            DeepSeekClient(FAKE_DEEPSEEK_KEY, opener=_Opener(reply_payload())).complete(
                model_call()
            )
            executor_with_rows().execute(request())
        env["LANGSMITH_PROJECT"] = "opspilot-lab-test"
        # Back in the outer Run: its own children still export.
        executor_with_rows().execute(request())
    tracer.shutdown()
    names = sorted(span.name for span in exporter.get_finished_spans())
    assert names == ["opspilot.run", "tool:metrics.range_query"]
    root = by_name(exporter.get_finished_spans())["opspilot.run"]
    assert dict(root.attributes)["langsmith.metadata.run_id"] == "outer"
    assert dict(root.attributes)["opspilot.run.tool_calls"] == 1
    assert dict(root.attributes)["opspilot.run.model_calls"] == 0


def test_finding_4_tool_request_params_are_not_copied_wholesale():
    exporter = InMemorySpanExporter()
    tracer = tracing.configure(lab_env(), exporter=exporter)
    with tracer.run(incident_id="i", run_id="r", versions={}):
        executor_with_rows().execute(
            request(
                params={
                    "expr": "rate(http_errors[5m])",
                    "Authorization": "Bearer " + FAKE_DEEPSEEK_KEY,
                    "api_token": "fake-token-value-2222",
                    "nested": {"password": "fake-password-3333"},
                },
                target_ref="checkout-prod",
            )
        )
    tracer.shutdown()
    text = all_attribute_text(exporter.get_finished_spans())
    for leaked in (FAKE_DEEPSEEK_KEY, "fake-token-value-2222", "fake-password-3333"):
        assert leaked not in text
    tool = dict(
        by_name(exporter.get_finished_spans())["tool:metrics.range_query"].attributes
    )
    prompt = json.loads(tool["gen_ai.prompt"])
    assert set(prompt) <= {"tool", "target", "window", "param_names"}
    assert "Authorization" not in prompt.get("param_names", [])
    assert "api_token" not in prompt.get("param_names", [])
    assert "expr" in prompt["param_names"]
    # The accepted query (declared parameters only) still travels on the outcome.
    assert "opspilot.tool.query" not in tool  # PARAM_NOT_ALLOWED: never accepted
    assert tool["opspilot.tool.reason"] == "PARAM_NOT_ALLOWED"


def test_finding_5_run_reason_exports_codes_only():
    exporter = InMemorySpanExporter()
    tracer = tracing.configure(lab_env(), exporter=exporter)
    free_text = "connection refused: password=fake-pw-4444 host=db.internal"
    with tracer.run(incident_id="i", run_id="r", versions={}) as span:
        span.settle("control_denied", free_text)
    with tracer.run(incident_id="i", run_id="r2", versions={}) as span:
        span.settle("handed_off", "LEASE_ACTIVE")
    with tracer.run(incident_id="i", run_id="r3", versions={}):
        with pytest.raises(RuntimeError):
            with tracer.model_call(model_call()):
                raise RuntimeError("secret-ish message fake-pw-5555")
    tracer.shutdown()
    spans = exporter.get_finished_spans()
    text = all_attribute_text(spans)
    assert "fake-pw-4444" not in text and "db.internal" not in text
    assert "fake-pw-5555" not in text
    reasons = {
        dict(s.attributes)["langsmith.metadata.run_id"]: dict(s.attributes).get(
            "opspilot.run.reason"
        )
        for s in spans
        if s.name == "opspilot.run"
    }
    assert reasons == {"r": "UNCLASSIFIED", "r2": "LEASE_ACTIVE", "r3": None}
    llm = by_name(spans)["deepseek.chat.completions"]
    assert dict(llm.attributes)["error.type"] == "RuntimeError"
    assert llm.status.description in (None, "")
    assert llm.events == ()


def test_finding_6_resource_ignores_ambient_otel_env(monkeypatch):
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "deployment.secret=fake-res-6666")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "not-opspilot")
    exporter = InMemorySpanExporter()
    one_lab_run(exporter)
    for span in exporter.get_finished_spans():
        assert dict(span.resource.attributes) == {"service.name": "opspilot"}


def test_finding_7_invalid_otel_env_does_not_break_startup(monkeypatch):
    monkeypatch.setenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "4096")
    monkeypatch.setenv("OTEL_BSP_MAX_QUEUE_SIZE", "not-a-number")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_COMPRESSION", "bogus")
    exporter = InMemorySpanExporter()
    tracer = tracing.configure(lab_env(), exporter=exporter)
    assert isinstance(tracer, Tracer)
    with tracer.run(incident_id="i", run_id="r", versions={}):
        pass
    tracer.shutdown()
    assert [s.name for s in exporter.get_finished_spans()] == ["opspilot.run"]


def test_finding_7_a_raising_setup_falls_back_to_null_tracer(caplog, monkeypatch):
    def boom(env):
        raise ValueError("exporter construction failed with fake-secret-7777")

    monkeypatch.setattr(tracing, "_langsmith_exporter", boom)
    with caplog.at_level(logging.ERROR, logger="opspilot.tracing"):
        tracer = tracing.configure(lab_env())
    assert isinstance(tracer, NullTracer)
    assert "TRACE_CONFIGURE_FAILED" in caplog.text
    assert "fake-secret-7777" not in caplog.text


# -- independent review round 2 (2026-10-05) -----------------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:secret@api.smith.langchain.com",
        "https://user@api.smith.langchain.com",
        "https://api.smith.langchain.com:8443",
        "https://api.smith.langchain.com/evil",
        "https://api.smith.langchain.com?x=1",
        "https://api.smith.langchain.com.attacker.example",
        "http://api.smith.langchain.com",
    ],
)
def test_round2_1_endpoint_rejects_userinfo_port_path_and_lookalikes(endpoint):
    assert "LANGSMITH_ENDPOINT_NOT_ALLOWED" in check_lab_target(
        lab_env(LANGSMITH_ENDPOINT=endpoint)
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://api.smith.langchain.com",
        "https://api.smith.langchain.com/",
        "https://API.Smith.LangChain.com.",
        "https://eu.api.smith.langchain.com:443",
    ],
)
def test_round2_1_canonical_endpoints_are_accepted(endpoint):
    assert check_lab_target(lab_env(LANGSMITH_ENDPOINT=endpoint)) == ()


def test_round2_2_unregistered_tool_and_target_names_are_not_exported():
    exporter = InMemorySpanExporter()
    tracer = tracing.configure(lab_env(), exporter=exporter)
    with tracer.run(incident_id="i", run_id="r", versions={}):
        outcome = executor_with_rows().execute(
            request(
                tool_name="Authorization=fake-tool-secret-8888",
                target_ref="fake-target-secret-9999",
            )
        )
    tracer.shutdown()
    assert outcome.status == "denied"
    spans = exporter.get_finished_spans()
    text = all_attribute_text(spans)
    assert "fake-tool-secret-8888" not in text
    assert "fake-target-secret-9999" not in text
    tool = by_name(spans)["tool:unknown"]
    attrs = dict(tool.attributes)
    assert "gen_ai.tool.name" not in attrs
    prompt = json.loads(attrs["gen_ai.prompt"])
    assert "tool" not in prompt and "target" not in prompt
    assert attrs["opspilot.tool.reason"] == "TOOL_NOT_REGISTERED"


def test_round2_2_registered_tool_with_unregistered_target_keeps_only_the_tool():
    exporter = InMemorySpanExporter()
    tracer = tracing.configure(lab_env(), exporter=exporter)
    with tracer.run(incident_id="i", run_id="r", versions={}):
        executor_with_rows().execute(request(target_ref="not-a-registered-target"))
    tracer.shutdown()
    spans = exporter.get_finished_spans()
    attrs = dict(by_name(spans)["tool:metrics.range_query"].attributes)
    assert attrs["gen_ai.tool.name"] == "metrics.range_query"
    assert "target" not in json.loads(attrs["gen_ai.prompt"])
    assert "not-a-registered-target" not in all_attribute_text(spans)


def test_round2_3_tool_call_arguments_redact_credential_like_keys():
    exporter = InMemorySpanExporter()
    tracer = tracing.configure(lab_env(), exporter=exporter)
    reply = json.loads(reply_payload())
    reply["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = (
        json.dumps(
            {
                "expr": "rate(http_errors[5m])",
                "api_key": "fake-arg-secret-1010",
                "nested": {
                    "Authorization": "fake-arg-secret-2020",
                    "keep": "visible-value",
                },
                "list": [{"password": "fake-arg-secret-5050"}],
            }
        )
    )
    prior = model_call()
    messages = list(prior.messages)
    messages[2] = {
        **messages[2],
        "tool_calls": [
            {
                "id": "call-0",
                "type": "function",
                "function": {
                    "name": "metrics.range_query",
                    "arguments": json.dumps(
                        {"token": "fake-arg-secret-3030", "expr": "up"}
                    ),
                },
            },
            {
                "id": "call-0b",
                "type": "function",
                "function": {
                    "name": "metrics.range_query",
                    "arguments": "not json fake-arg-plain-4040",
                },
            },
        ],
    }
    call = model_call(messages=tuple(messages))
    with tracer.run(incident_id="i", run_id="r", versions={}):
        DeepSeekClient(
            FAKE_DEEPSEEK_KEY, opener=_Opener(json.dumps(reply).encode())
        ).complete(call)
    tracer.shutdown()
    text = all_attribute_text(exporter.get_finished_spans())
    for leaked in (
        "fake-arg-secret-1010",
        "fake-arg-secret-2020",
        "fake-arg-secret-3030",
        "fake-arg-secret-5050",
    ):
        assert leaked not in text
    assert "visible-value" in text and "rate(http_errors[5m])" in text
    assert "[REDACTED]" in text
    # Unparseable arguments are exported as-is (bounded): documented in the task record.
    assert "fake-arg-plain-4040" in text
