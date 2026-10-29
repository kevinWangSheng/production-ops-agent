"""judge-adapters-4: one retry after a stream transport failure, nothing else.

Authority: docs/tasks/2026-10-20-judge-streaming.md R1-R5 (2026-10-21 user
decision: retry once only when no complete response arrived, i.e. the
connection failed or the stream ended before ``[DONE]``). Every response is
served by an in-process ``httpx2.MockTransport``; nothing opens a socket.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace

import httpx2
import pytest
from eval_b3_support import (
    MODELS,
    FakeTransport,
    NetworkGuard,
    completion,
    env_text,
    http,
    judge_input,
    load,
    write_env,
)
from judge_stream_support import (
    ANSWER,
    Pieces,
    Wire,
    answer_chunks,
    chunk,
    data_event,
    split,
    sse_body,
)

from opspilot.evaluation.calibration_flow import (
    CalibrationObservation,
    _metadata_identifiers,
)
from opspilot.evaluation.codec import decode, encode
from opspilot.evaluation.judge import (
    HTTPJudgeTransport,
    JudgeError,
    JudgeObservation,
    check_config,
    judge_config_hash,
    judge_once,
)

PROVIDERS = ("zai", "openai", "deepseek")


@pytest.fixture(scope="module", autouse=True)
def network_guard():
    guard = NetworkGuard()
    with pytest.MonkeyPatch.context() as mp:
        guard.install(mp)
        yield guard
    assert guard.attempts == [], f"network attempted: {guard.attempts}"


@pytest.fixture(scope="module")
def configs(tmp_path_factory):
    path = write_env(tmp_path_factory.mktemp("judge-retry-env"), env_text())
    return {provider: load(path, provider) for provider in PROVIDERS}


Responder = Callable[[httpx2.Request], httpx2.Response]


def sse(body: bytes | Pieces, *, status: int = 200) -> Responder:
    content_type = {"content-type": "text/event-stream"}

    def respond(_request: httpx2.Request) -> httpx2.Response:
        stream = Pieces(split(body, 64)) if isinstance(body, bytes) else body
        return httpx2.Response(status, headers=content_type, stream=stream)

    return respond


def raising(error: Exception) -> Responder:
    def respond(_request: httpx2.Request) -> httpx2.Response:
        raise error

    return respond


class Sequence:
    """Serves the n-th responder on the n-th request; records every request."""

    def __init__(self, *responders: Responder) -> None:
        self.responders = responders
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        index = min(len(self.requests), len(self.responders)) - 1
        return self.responders[index](request)

    def transport(self) -> HTTPJudgeTransport:
        return HTTPJudgeTransport(transport=httpx2.MockTransport(self))


def run(configs, wire, provider="zai", **changes):
    config = replace(configs[provider], **changes)
    return judge_once(
        judge_input(), config=config, calibration=None, transport=wire.transport()
    )


def good(provider="zai") -> Responder:
    return sse(sse_body(answer_chunks(provider)))


def drop_before_done(provider="zai") -> Responder:
    """Stream ends cleanly but without ``data: [DONE]`` (S8)."""
    return sse(sse_body(answer_chunks(provider), done=False))


def peer_closed(provider="zai") -> Responder:
    """The observed failure: bytes flow, then the peer closes mid-body."""
    body = sse_body(answer_chunks(provider)[:2], done=False)
    error = httpx2.RemoteProtocolError(
        "peer closed connection without sending complete message body"
    )
    return lambda _r: httpx2.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=Pieces(split(body, 9), then_raise=error),
    )


def assert_ok(observation, attempts):
    assert observation.error_code is None, observation.error_code
    assert observation.result.status == "ok" and observation.valid is True
    assert observation.attempts == attempts


def assert_failed(observation, status, code, attempts):
    assert (observation.result.status, observation.error_code) == (status, code)
    assert observation.valid is False and observation.passed is False
    assert observation.attempts == attempts


# ------------------------------------------------- R1: what is retried, once


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "first",
    [
        drop_before_done,
        peer_closed,
        lambda _p: raising(httpx2.ReadError("read failed")),
        lambda _p: raising(httpx2.ConnectError("connect failed")),
    ],
    ids=["eof_before_done", "peer_closed", "read_error", "connect_error"],
)
def test_transport_failure_then_success_is_ok_after_one_retry(configs, provider, first):
    wire = Sequence(first(provider), good(provider))
    observation = run(configs, wire, provider)
    assert_ok(observation, attempts=2)
    assert observation.response_model == MODELS[provider]
    assert observation.finish_reason == "stop"
    one, two = wire.requests
    assert one.content == two.content  # identical body
    assert one.url == two.url and one.method == two.method == "POST"
    assert dict(one.headers) == dict(two.headers)  # identical headers


def test_two_transport_failures_stop_after_the_second_call(configs):
    wire = Sequence(peer_closed(), drop_before_done(), good())
    observation = run(configs, wire)
    assert_failed(observation, "error", "JUDGE_TRANSPORT_ERROR", attempts=2)
    assert len(wire.requests) == 2
    assert observation.usage == dict.fromkeys(observation.usage)  # nothing assembled
    assert observation.finish_reason is None


def test_first_attempt_success_is_one_attempt(configs):
    wire = Sequence(good())
    assert_ok(run(configs, wire), attempts=1)
    assert len(wire.requests) == 1


# ------------------------------------------------- R1: what is never retried


def test_timeout_is_not_retried(configs):
    stall = Pieces([b": keep-alive\n\n"], then_sleep=5.0)
    wire = Sequence(sse(stall), good())
    observation = run(configs, wire, timeout_seconds=0.3)
    assert_failed(observation, "timeout", "JUDGE_TIMEOUT", attempts=1)
    assert len(wire.requests) == 1


@pytest.mark.parametrize("status", [400, 401, 429, 500, 502, 503])
def test_http_error_status_is_not_retried(configs, status):
    wire = Sequence(sse(b'{"error":"busy"}', status=status), good())
    observation = run(configs, wire)
    assert_failed(observation, "error", "JUDGE_HTTP_ERROR", attempts=1)
    assert len(wire.requests) == 1


@pytest.mark.parametrize("status", [429, 500, 503])
def test_http_error_body_cut_short_is_not_retried(configs, status):
    """A known non-2xx status stays JUDGE_HTTP_ERROR even if its body breaks."""
    broken = Pieces(
        [b'{"error":'], then_raise=httpx2.RemoteProtocolError("peer closed")
    )
    wire = Sequence(sse(broken, status=status), good())
    observation = run(configs, wire)
    assert_failed(observation, "error", "JUDGE_HTTP_ERROR", attempts=1)
    assert len(wire.requests) == 1


def test_in_stream_error_event_is_not_retried(configs):
    body = data_event(answer_chunks()[0]) + data_event({"error": {"code": "x"}})
    wire = Sequence(sse(body), good())
    observation = run(configs, wire)
    assert_failed(observation, "error", "JUDGE_HTTP_ERROR", attempts=1)
    assert len(wire.requests) == 1


def test_oversize_is_not_retried(configs):
    wire = Sequence(good(), good())
    observation = run(configs, wire, max_response_bytes=64)
    assert_failed(observation, "oversize", "JUDGE_RESPONSE_OVERSIZE", attempts=1)
    assert len(wire.requests) == 1


@pytest.mark.parametrize(
    "events,code",
    [
        (answer_chunks(finish="length"), "JUDGE_OUTPUT_INVALID"),
        (answer_chunks(text="not-json"), "SCHEMA_INVALID"),
        (
            [chunk(delta={"role": "assistant", "content": ANSWER}), chunk()],
            "JUDGE_OUTPUT_INVALID",  # complete stream, no finish_reason
        ),
    ],
    ids=["finish_length", "content_not_json", "no_finish"],
)
def test_complete_but_invalid_document_is_not_retried(configs, events, code):
    wire = Sequence(sse(sse_body(events)), good())
    observation = run(configs, wire)
    assert_failed(observation, "invalid", code, attempts=1)
    assert len(wire.requests) == 1


def test_identity_error_on_complete_document_is_not_retried(configs):
    events = answer_chunks()
    for event in events:
        event["model"] = "some-other-model"
    wire = Sequence(sse(sse_body(events)), good())
    observation = run(configs, wire)
    assert_failed(observation, "error", "JUDGE_IDENTITY_MISMATCH", attempts=1)
    assert len(wire.requests) == 1


def test_non_event_stream_200_is_not_retried(configs):
    wire = Sequence(lambda _r: httpx2.Response(200, json={"choices": []}), good())
    observation = run(configs, wire)
    assert_failed(observation, "invalid", "JUDGE_OUTPUT_INVALID", attempts=1)
    assert len(wire.requests) == 1


def test_retry_failing_differently_reports_the_final_attempt(configs):
    wire = Sequence(peer_closed(), sse(b"busy", status=429))
    observation = run(configs, wire)
    assert_failed(observation, "error", "JUDGE_HTTP_ERROR", attempts=2)
    assert len(wire.requests) == 2


# --------------------------------------- fake transports (B3 JudgeTransport)


def test_fake_transport_judge_error_transport_code_is_retried(configs):
    replies = iter([JudgeError("JUDGE_TRANSPORT_ERROR")])
    fake = FakeTransport(
        lambda _c: next(replies, http(completion(ANSWER, model=MODELS["zai"])))
    )
    observation = judge_once(
        judge_input(), config=configs["zai"], calibration=None, transport=fake
    )
    assert_ok(observation, attempts=2)
    assert len(fake.calls) == 2
    assert fake.calls[0] == fake.calls[1]  # same endpoint/headers/body/limits


@pytest.mark.parametrize(
    "error,status,code",
    [
        (JudgeError("JUDGE_HTTP_ERROR"), "error", "JUDGE_HTTP_ERROR"),
        (JudgeError("JUDGE_OUTPUT_INVALID"), "invalid", "JUDGE_OUTPUT_INVALID"),
        (JudgeError("JUDGE_RESPONSE_OVERSIZE"), "oversize", "JUDGE_RESPONSE_OVERSIZE"),
        (TimeoutError(), "timeout", "JUDGE_TIMEOUT"),
    ],
)
def test_fake_transport_non_transport_failures_are_not_retried(
    configs, error, status, code
):
    fake = FakeTransport(lambda _c: error)
    observation = judge_once(
        judge_input(), config=configs["zai"], calibration=None, transport=fake
    )
    assert_failed(observation, status, code, attempts=1)
    assert len(fake.calls) == 1


# ---------------------------------------------------- R2: per-attempt budget


def test_timeout_caps_each_attempt_and_elapsed_is_total(configs):
    slow_drop = Pieces([b": keep-alive\n\n"], then_sleep=0.5)  # EOF, no [DONE]
    slow_good = Pieces([sse_body(answer_chunks())], delay=0.5)
    wire = Sequence(sse(slow_drop), sse(slow_good))
    started = time.monotonic()
    observation = run(configs, wire, timeout_seconds=0.8)
    assert_ok(observation, attempts=2)
    assert observation.elapsed_seconds > 0.8  # > one cap, each attempt < 0.8
    assert observation.elapsed_seconds <= time.monotonic() - started


def test_retry_attempt_that_times_out_is_a_timeout(configs):
    wire = Sequence(peer_closed(), sse(Pieces([b": ka\n\n"], then_sleep=5.0)))
    observation = run(configs, wire, timeout_seconds=0.3)
    assert_failed(observation, "timeout", "JUDGE_TIMEOUT", attempts=2)
    assert len(wire.requests) == 2


# ---------------------------------------------- R3: observation and decoding


def test_attempts_round_trips_and_legacy_observation_decodes_as_one(configs):
    observation = run(configs, Sequence(drop_before_done(), good()))
    current = encode(observation)
    assert current["attempts"] == 2
    assert decode(JudgeObservation, current).attempts == 2
    _metadata_identifiers(observation)  # calibration identifier policy accepts it
    legacy = {k: v for k, v in current.items() if k not in {"attempts"}}
    assert decode(JudgeObservation, legacy).attempts == 1
    older = {k: v for k, v in legacy.items() if k != "finish_reason"}
    assert decode(JudgeObservation, older).attempts == 1
    record = {"item_id": "item-1", "input_hash": "0" * 64, "repetition": 1}
    stored = decode(CalibrationObservation, {**record, "observation": legacy})
    assert stored.observation.attempts == 1
    retried = decode(CalibrationObservation, {**record, "observation": current})
    assert retried.observation.attempts == 2


def test_pre_send_failure_reports_one_attempt_and_no_call(configs):
    wire = Sequence(good())
    observation = run(configs, wire, adapter_version="judge-adapters-2")
    assert_failed(observation, "invalid", "CONFIG_INVALID", attempts=1)
    assert wire.requests == []


# ------------------------------------------------------- R4: adapter version


def test_adapter_version_is_judge_adapters_7(configs):
    for config in configs.values():
        assert config.adapter_version == "judge-adapters-7"  # P7 r3 D20
        check_config(config)
    for stale in (
        "judge-adapters-1",
        "judge-adapters-2",
        "judge-adapters-3",
        "judge-adapters-4",
        "judge-adapters-5",
        "judge-adapters-6",  # P7 r3 D20
    ):
        with pytest.raises(JudgeError, match="CONFIG_INVALID"):
            check_config(replace(configs["zai"], adapter_version=stale))
    previous = replace(configs["zai"], adapter_version="judge-adapters-2")
    assert judge_config_hash(previous) != judge_config_hash(configs["zai"])


def test_wire_fixture_reuse_drop_twice_matches_streaming_contract(configs):
    """The single-response Wire fixture now sees two identical requests."""
    wire = Wire(sse_body(answer_chunks(), done=False))
    observation = run(configs, wire)
    assert_failed(observation, "error", "JUDGE_TRANSPORT_ERROR", attempts=2)
    assert len(wire.requests) == 2
    assert wire.requests[0].content == wire.requests[1].content
