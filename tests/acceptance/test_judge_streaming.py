"""Offline acceptance tests for the judge SSE contract (judge-streaming S1-S15).

Authority: docs/tasks/2026-10-20-judge-streaming.md (decision table r0, cited
S<n>) and the B3 interface it amends. Only public results are asserted:
``judge_once`` observations (status, error_code, usage, finish_reason) and the
assembled non-streaming completion returned by ``HTTPJudgeTransport.send``
(S3). Responses come from ``httpx2.MockTransport``; the network guard refuses
every socket connect and DNS lookup.
"""

from __future__ import annotations

import dataclasses
import json
import time
from dataclasses import replace

import httpx2
import pytest
from eval_b3_support import (
    MODELS,
    USAGE,
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
    DONE,
    KIB,
    MIB,
    Pieces,
    Wire,
    answer_chunks,
    chunk,
    data_event,
    split,
    sse_body,
)

from opspilot.evaluation.calibration_flow import CalibrationObservation
from opspilot.evaluation.codec import canonical_bytes, decode, encode
from opspilot.evaluation.judge import (
    _ADAPTERS,
    PROMPT,
    REPORT_FIELDS_FOOTER,
    REPORT_FIELDS_HEADER,
    JudgeError,
    JudgeObservation,
    check_config,
    judge_once,
    safe_input,
)
from opspilot.evaluation.model import JudgedDiagnosis
from opspilot.evaluation.runs import _location_text
from opspilot.investigation.reports import parse_report

PROVIDERS = ("zai", "openai", "deepseek")
IDENTITY_CODE = {  # B3:20/30 - GLM/DeepSeek identity = error, OpenAI = invalid
    "zai": ("error", "JUDGE_IDENTITY_MISMATCH"),
    "deepseek": ("error", "JUDGE_IDENTITY_MISMATCH"),
    "openai": ("invalid", "JUDGE_RESPONSE_IDENTITY_INVALID"),
}


@pytest.fixture(scope="module", autouse=True)
def network_guard():
    guard = NetworkGuard()
    with pytest.MonkeyPatch.context() as mp:
        guard.install(mp)
        yield guard
    assert guard.attempts == [], f"network attempted: {guard.attempts}"


@pytest.fixture(scope="module")
def configs(tmp_path_factory):
    path = write_env(tmp_path_factory.mktemp("judge-stream-env"), env_text())
    return {provider: load(path, provider) for provider in PROVIDERS}


def run(configs, wire, provider="zai", **changes):
    config = replace(configs[provider], **changes)
    return judge_once(
        judge_input(), config=config, calibration=None, transport=wire.transport()
    )


def assert_ok(observation):
    assert observation.error_code is None, observation.error_code
    assert observation.result.status == "ok"
    assert observation.valid is True


def assert_failed(observation, status, code):
    assert (observation.result.status, observation.error_code) == (status, code)
    assert observation.valid is False and observation.passed is False


def fake_ok():
    return FakeTransport(
        lambda _c: http(completion(ANSWER, model=MODELS["zai"], usage=USAGE))
    )


# ------------------------------------------------------------ fixture sanity


def test_fixture_answer_is_ok_through_non_streaming_fake(configs):
    """S3: fake transports keep returning assembled JSON; proves ANSWER is valid."""
    observation = judge_once(
        judge_input(), config=configs["zai"], calibration=None, transport=fake_ok()
    )
    assert_ok(observation)
    assert observation.usage == USAGE


# ------------------------------------------- S3 assembled completion document


@pytest.mark.parametrize("provider", PROVIDERS)
def test_send_returns_assembled_non_streaming_completion(provider):
    events = answer_chunks(provider, openai_usage_tail=provider == "openai")
    events[1]["choices"][0]["delta"]["reasoning_content"] = "private-reasoning-marker"
    wire = Wire(sse_body(events), piece=13)
    response = wire.transport().send(
        endpoint="https://judge.invalid/chat/completions",
        headers={"Authorization": "Bearer canary", "Content-Type": "application/json"},
        body=b"{}",
        timeout_seconds=30.0,
        max_response_bytes=2 * MIB,
    )
    assert len(wire.requests) == 1
    assert response.status_code == 200
    raw = b"".join(response.chunks)
    assert b"private-reasoning-marker" not in raw  # S12: reasoning discarded
    doc = json.loads(raw)
    assert doc["model"] == MODELS[provider]
    (choice,) = doc["choices"]
    assert choice["index"] == 0 and choice["finish_reason"] == "stop"
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"] == ANSWER
    assert doc["usage"] == USAGE


# --------------------------------------------------------- S7 SSE framing


@pytest.mark.parametrize("eol", [b"\n", b"\r\n", b"\r"])
@pytest.mark.parametrize("piece", [None, 1, 7, 31])
def test_keepalive_comments_line_endings_and_cross_chunk_splits(configs, eol, piece):
    body = b": keep-alive" + eol + eol
    for event in answer_chunks():
        body += b":" + eol + data_event(event, eol=eol) + b": keep-alive" + eol + eol
    body += b"data: [DONE]" + eol + eol
    observation = run(configs, Wire(body, piece=piece))
    assert_ok(observation)
    assert observation.usage == USAGE
    assert observation.finish_reason == "stop"


def test_multiline_data_joined_with_newline_and_ignored_fields(configs):
    body = b"retry: 1000\n"
    for event in answer_chunks():
        pretty = json.dumps(event, indent=1).encode().split(b"\n")
        # one event over several data: lines; first without the optional space
        body += b"id: 7\nevent: message\ndata:" + pretty[0] + b"\n"
        body += b"".join(b"data: " + line + b"\n" for line in pretty[1:]) + b"\n"
    body += DONE
    assert_ok(run(configs, Wire(body, piece=5)))


def test_utf8_split_inside_multibyte_character_is_buffered(configs):
    events = answer_chunks()
    events[1]["choices"][0]["delta"]["x_note"] = "诊断中" * 50  # unknown key ignored
    raw = sse_body([json.dumps(e, ensure_ascii=False).encode() for e in events])
    assert "诊".encode() in raw
    assert_ok(run(configs, Wire(raw, piece=1)))


def test_invalid_utf8_line_is_output_invalid(configs):
    events = [json.dumps(e).encode() for e in answer_chunks()]
    events.insert(2, json.dumps(chunk()).encode()[:-1] + b',"x":"\xff\xfe"}')
    observation = run(configs, Wire(sse_body(events)))
    assert_failed(observation, "invalid", "JUDGE_OUTPUT_INVALID")


def test_non_message_event_type_is_output_invalid(configs):
    body = sse_body(answer_chunks(), done=False)
    body = b"event: ping\ndata: {}\n\n" + body + DONE
    observation = run(configs, Wire(body))
    assert_failed(observation, "invalid", "JUDGE_OUTPUT_INVALID")


# ---------------------------------------------------------------- S8 [DONE]


def test_eof_before_done_is_transport_error(configs):
    observation = run(configs, Wire(sse_body(answer_chunks(), done=False)))
    assert_failed(observation, "error", "JUDGE_TRANSPORT_ERROR")


def test_disconnect_mid_stream_is_transport_error(configs):
    body = sse_body(answer_chunks()[:2], done=False)
    stream = Pieces(split(body, 9), then_raise=httpx2.ReadError("peer reset"))
    observation = run(configs, Wire(stream))
    assert_failed(observation, "error", "JUDGE_TRANSPORT_ERROR")


def test_done_stops_reading_trailing_bytes_and_open_connection(configs):
    """S8: after [DONE] nothing more is read, even garbage or a stalled socket."""
    body = sse_body(answer_chunks()) + b"data: {not json\n\nevent: ping\n\n"
    stalled = Pieces(split(body, 11), then_sleep=30.0)
    started = time.monotonic()
    observation = run(configs, Wire(stalled), timeout_seconds=10)
    assert_ok(observation)
    assert time.monotonic() - started < 8


# --------------------------------------------------- S9 / S10 status & type


@pytest.mark.parametrize("content_type", ["application/json", "text/plain", None])
def test_200_without_event_stream_is_output_invalid(configs, content_type):
    # Even a well-formed non-streaming completion is not accepted (no fallback).
    body = completion(ANSWER, model=MODELS["zai"], usage=USAGE)
    observation = run(configs, Wire(body, content_type=content_type))
    assert_failed(observation, "invalid", "JUDGE_OUTPUT_INVALID")


def test_event_stream_content_type_parameters_are_ignored(configs):
    wire = Wire(
        sse_body(answer_chunks()), content_type="text/event-stream; charset=utf-8"
    )
    assert_ok(run(configs, wire))


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
@pytest.mark.parametrize("content_type", ["application/json", "text/event-stream"])
def test_non_2xx_is_http_error_without_sse_parsing(configs, status, content_type):
    body = (
        sse_body(answer_chunks())
        if content_type == "text/event-stream"
        else b'{"error":{"message":"rate limited"}}'
    )
    observation = run(configs, Wire(body, status=status, content_type=content_type))
    assert_failed(observation, "error", "JUDGE_HTTP_ERROR")


# ------------------------------------------------------ S11 in-stream error


@pytest.mark.parametrize("position", [0, 2])
def test_in_stream_error_event_is_http_error(configs, position):
    events = answer_chunks()
    events.insert(position, {"error": {"message": "upstream overloaded"}})
    observation = run(configs, Wire(sse_body(events)))
    assert_failed(observation, "error", "JUDGE_HTTP_ERROR")


@pytest.mark.parametrize("raw", [b"[1, 2]", b'"text"', b"{not json", b"null"])
def test_non_object_or_bad_json_event_is_output_invalid(configs, raw):
    events = [json.dumps(e).encode() for e in answer_chunks()]
    events.insert(1, raw)
    observation = run(configs, Wire(sse_body(events)))
    assert_failed(observation, "invalid", "JUDGE_OUTPUT_INVALID")


# ----------------------------------------------------- S12 delta assembly


def test_role_may_arrive_only_on_final_chunk(configs):
    """Z.ai streaming example: delta.role="assistant" on the last chunk."""
    events = [
        chunk(delta={"content": ANSWER[:10]}),
        chunk(delta={"content": ANSWER[10:]}),
        chunk(delta={"role": "assistant"}, finish="stop", usage=USAGE),
    ]
    assert_ok(run(configs, Wire(sse_body(events))))


def test_missing_role_is_output_invalid(configs):
    events = answer_chunks()
    del events[0]["choices"][0]["delta"]["role"]
    observation = run(configs, Wire(sse_body(events)))
    assert_failed(observation, "invalid", "JUDGE_OUTPUT_INVALID")


@pytest.mark.parametrize("role", ["user", "system", "tool", ""])
def test_wrong_role_is_output_invalid(configs, role):
    events = answer_chunks()
    events[2]["choices"][0]["delta"]["role"] = role
    observation = run(configs, Wire(sse_body(events)))
    assert_failed(observation, "invalid", "JUDGE_OUTPUT_INVALID")


@pytest.mark.parametrize(
    "delta",
    [
        {"tool_calls": [{"index": 0, "id": "call_1", "type": "function"}]},
        {"tool_calls": []},
        {"function_call": {"name": "f", "arguments": ""}},
        {"refusal": "I cannot help with that."},
    ],
)
def test_tool_calls_function_call_or_refusal_is_output_invalid(configs, delta):
    events = answer_chunks()
    events[2]["choices"][0]["delta"].update(delta)
    observation = run(configs, Wire(sse_body(events)))
    assert_failed(observation, "invalid", "JUDGE_OUTPUT_INVALID")


def test_null_tool_fields_and_unknown_delta_keys_are_accepted(configs):
    events = answer_chunks()
    events[1]["choices"][0]["delta"].update(
        {"tool_calls": None, "function_call": None, "refusal": None, "x_vendor": 1}
    )
    events[1]["choices"][0]["logprobs"] = None
    assert_ok(run(configs, Wire(sse_body(events))))


# ------------------------------------------------------ S2 size limits


def test_reasoning_over_two_mib_is_discarded_and_small_content_is_ok(configs):
    """S2a: max_response_bytes measures the assembled document, not the wire."""
    events = answer_chunks()
    # each event < 1 MiB, total reasoning > 2 MiB, wire < 32 MiB
    events[1:1] = [
        chunk(delta={"reasoning_content": "r" * (900 * KIB)}) for _ in range(3)
    ]
    body = sse_body(events)
    assert len(body) > 2 * MIB
    observation = run(configs, Wire(body, piece=64 * KIB))
    assert_ok(observation)
    assert observation.usage == USAGE


def test_assembled_document_limit_still_applies(configs):
    """S2a: the config limit now bounds the assembled completion document."""
    events = answer_chunks()
    events[1:1] = [chunk(delta={"reasoning_content": "r" * (64 * KIB)})]
    body = sse_body(events)
    small = completion(ANSWER, model=MODELS["zai"], usage=USAGE)
    observation = run(configs, Wire(body), max_response_bytes=len(small) + 1024)
    assert_ok(observation)  # wire bytes far above the limit, document below
    observation = run(configs, Wire(body), max_response_bytes=64)
    assert_failed(observation, "oversize", "JUDGE_RESPONSE_OVERSIZE")


def test_single_event_over_one_mib_is_oversize(configs):
    events = answer_chunks()
    events[1:1] = [chunk(delta={"reasoning_content": "r" * (MIB + 4 * KIB)})]
    observation = run(configs, Wire(sse_body(events), piece=64 * KIB))
    assert_failed(observation, "oversize", "JUDGE_RESPONSE_OVERSIZE")


def test_wire_over_32_mib_is_oversize(configs):
    head = sse_body(answer_chunks()[:1], done=False)
    filler = data_event(chunk(delta={"reasoning_content": "r" * (900 * KIB)}))
    tail = sse_body(answer_chunks()[1:])
    count = (34 * MIB) // len(filler) + 1

    def pieces():
        yield head
        for _ in range(count):
            yield filler
        yield tail

    observation = run(configs, Wire(Pieces(pieces)))
    assert_failed(observation, "oversize", "JUDGE_RESPONSE_OVERSIZE")


def test_keepalive_comments_count_toward_wire_limit(configs):
    comment = b":" + b" " * (512 * KIB) + b"\n"
    count = (34 * MIB) // len(comment) + 1

    def pieces():
        for _ in range(count):
            yield comment
        yield sse_body(answer_chunks())

    observation = run(configs, Wire(Pieces(pieces)))
    assert_failed(observation, "oversize", "JUDGE_RESPONSE_OVERSIZE")


# --------------------------------------- S13 finish_reason / usage / choices


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("tail", ["usage_on_finish_chunk", "openai_empty_choices"])
def test_usage_shapes_and_finish_reason_recorded(configs, provider, tail):
    events = answer_chunks(provider, openai_usage_tail=tail == "openai_empty_choices")
    observation = run(configs, Wire(sse_body(events)), provider)
    assert_ok(observation)
    assert observation.usage == USAGE
    assert observation.finish_reason == "stop"


def test_usage_is_last_non_null_object(configs):
    early = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
    events = answer_chunks()
    events[1]["usage"] = early
    observation = run(configs, Wire(sse_body(events)))
    assert_ok(observation)
    assert observation.usage == USAGE  # later non-null wins

    events = answer_chunks(usage=None)
    events[1]["usage"] = early
    events[2]["usage"] = None
    observation = run(configs, Wire(sse_body(events)))
    assert_ok(observation)
    assert observation.usage == early  # trailing nulls do not erase it


def test_missing_usage_is_unknown_not_failure(configs):
    observation = run(configs, Wire(sse_body(answer_chunks(usage=None))))
    assert_ok(observation)
    assert dict(observation.usage) == dict.fromkeys(
        ("prompt_tokens", "completion_tokens", "total_tokens")
    )


def test_finish_reason_twice_is_output_invalid(configs):
    events = answer_chunks()
    events.append(chunk(finish="stop"))
    observation = run(configs, Wire(sse_body(events)))
    assert_failed(observation, "invalid", "JUDGE_OUTPUT_INVALID")


def test_no_finish_reason_is_output_invalid(configs):
    events = answer_chunks()
    events[-1]["choices"][0]["finish_reason"] = None
    observation = run(configs, Wire(sse_body(events)))
    assert_failed(observation, "invalid", "JUDGE_OUTPUT_INVALID")


def test_content_after_finish_is_output_invalid(configs):
    events = answer_chunks()
    events.append(chunk(delta={"content": " "}))
    observation = run(configs, Wire(sse_body(events)))
    assert_failed(observation, "invalid", "JUDGE_OUTPUT_INVALID")


def test_empty_choices_before_finish_is_output_invalid(configs):
    events = answer_chunks()
    events.insert(2, chunk(choices=[]))
    observation = run(configs, Wire(sse_body(events)))
    assert_failed(observation, "invalid", "JUDGE_OUTPUT_INVALID")


@pytest.mark.parametrize(
    "choices",
    [
        [{"index": 1, "delta": {"content": "x"}, "finish_reason": None}],
        [
            {"index": 0, "delta": {"content": "x"}, "finish_reason": None},
            {"index": 1, "delta": {"content": "y"}, "finish_reason": None},
        ],
    ],
)
def test_non_zero_index_or_multiple_choices_is_output_invalid(configs, choices):
    events = answer_chunks()
    events.insert(2, chunk(choices=choices))
    observation = run(configs, Wire(sse_body(events)))
    assert_failed(observation, "invalid", "JUDGE_OUTPUT_INVALID")


# ------------------------------------- S6 observability of failed responses


@pytest.mark.parametrize(
    "finish", ["length", "network_error", "sensitive", "insufficient_system_resource"]
)
def test_non_stop_finish_is_invalid_but_usage_and_reason_recorded(configs, finish):
    observation = run(configs, Wire(sse_body(answer_chunks(finish=finish))))
    assert_failed(observation, "invalid", "JUDGE_OUTPUT_INVALID")
    assert observation.usage == USAGE
    assert observation.finish_reason == finish


def test_unparseable_content_is_invalid_but_usage_and_reason_recorded(configs):
    events = answer_chunks(text='{"schema": "opspilot-judge-1", "diag')
    observation = run(configs, Wire(sse_body(events)))
    assert observation.result.status == "invalid" and observation.valid is False
    assert observation.error_code in {"JUDGE_OUTPUT_INVALID", "SCHEMA_INVALID"}
    assert observation.usage == USAGE
    assert observation.finish_reason == "stop"


def test_observation_finish_reason_defaults_to_none_and_legacy_records_decode(
    configs,
):
    """S6: new optional field; artifacts written before it decode as None."""
    observation = judge_once(
        judge_input(), config=configs["zai"], calibration=None, transport=fake_ok()
    )
    current = encode(observation)
    assert current["finish_reason"] == "stop"
    assert decode(JudgeObservation, current).finish_reason == "stop"
    legacy = {k: v for k, v in current.items() if k != "finish_reason"}
    assert decode(JudgeObservation, legacy).finish_reason is None
    record = {"item_id": "item-1", "input_hash": "0" * 64, "repetition": 1}
    stored = decode(CalibrationObservation, {**record, "observation": legacy})
    assert stored.observation.finish_reason is None
    built = JudgeObservation(
        observation.result,
        observation.valid,
        observation.recomputed_verdict,
        observation.passed,
        observation.error_code,
        observation.response_model,
        observation.usage,
        observation.elapsed_seconds,
        observation.provider,
    )
    assert built.finish_reason is None


# ------------------------------------------------------ S14 response identity


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("mutation", ["model_changes", "id_changes", "model_missing"])
def test_identity_must_be_consistent_across_chunks(configs, provider, mutation):
    events = answer_chunks(provider)
    target = events[2]
    if mutation == "model_changes":
        target["model"] = MODELS[provider] + "-other"
    elif mutation == "id_changes":
        target["id"] = "chatcmpl-stream-2"
    else:
        del target["model"]
    observation = run(configs, Wire(sse_body(events)), provider)
    assert_failed(observation, *IDENTITY_CODE[provider])


@pytest.mark.parametrize("provider", PROVIDERS)
def test_consistent_but_wrong_model_uses_existing_identity_code(configs, provider):
    events = answer_chunks(provider)
    for event in events:
        event["model"] = "some-other-model"
    observation = run(configs, Wire(sse_body(events)), provider)
    assert_failed(observation, *IDENTITY_CODE[provider])


def test_openai_dated_snapshot_consistent_across_chunks_is_ok(configs):
    events = answer_chunks("openai", openai_usage_tail=True)
    for event in events:
        event["model"] = "gpt-5.6-luna-2026-09-30"
    observation = run(configs, Wire(sse_body(events)), "openai")
    assert_ok(observation)
    assert observation.response_model == "gpt-5.6-luna-2026-09-30"


# ----------------------------------------------- S1 / S4 / S5 request side


@pytest.mark.parametrize("provider", PROVIDERS)
def test_request_streams_with_provider_stream_options_and_accept(configs, provider):
    wire = Wire(sse_body(answer_chunks(provider)))
    observation = run(configs, wire, provider)
    assert_ok(observation)
    (request,) = wire.requests
    assert request.headers["accept"] == "text/event-stream"
    body = json.loads(request.content)
    assert body["stream"] is True
    if provider in {"openai", "deepseek"}:
        assert body["stream_options"] == {"include_usage": True}
    else:
        assert "stream_options" not in body
    # zai runs GLM-5.3 at reasoning_effort=high (user decision 2026-10-21).
    if provider == "zai":
        assert body["reasoning_effort"] == "high"
    else:
        assert "reasoning_effort" not in body


_DIAGNOSIS_TYPES = {
    "location": "string",
    "quote": "string",
    "factual_causal": "boolean",
    "service": ["string", "null"],
    "operation": ["string", "null"],
    "cited_evidence_ids": "array",
    "linked_claim": ["integer", "null"],
    "supported": "boolean",
    "reason": "string",
}


@pytest.mark.parametrize("provider", PROVIDERS)
def test_request_enforces_json_output_per_provider(configs, provider):
    """judge-adapters-5: OpenAI strict json_schema, Z.ai json_object, DeepSeek none."""
    wire = Wire(sse_body(answer_chunks(provider)))
    assert_ok(run(configs, wire, provider))
    (request,) = wire.requests
    body = json.loads(request.content)
    if provider == "deepseek":
        assert "response_format" not in body
        return
    if provider == "zai":
        assert body["response_format"] == {"type": "json_object"}
        return
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "opspilot_judge_1"
    assert fmt["json_schema"]["strict"] is True
    top = fmt["json_schema"]["schema"]
    assert top["type"] == "object" and top["additionalProperties"] is False
    assert (
        set(top["required"])
        == set(top["properties"])
        == {
            "schema",
            "diagnoses",
            "verdict",
        }
    )
    assert top["properties"]["schema"]["enum"] == ["opspilot-judge-1"]
    assert top["properties"]["verdict"] == {"type": "boolean"}
    item = top["properties"]["diagnoses"]["items"]
    fields = [f.name for f in dataclasses.fields(JudgedDiagnosis)]
    assert item["additionalProperties"] is False
    assert sorted(item["required"]) == sorted(item["properties"]) == sorted(fields)
    assert {k: v["type"] for k, v in item["properties"].items()} == _DIAGNOSIS_TYPES
    assert item["properties"]["cited_evidence_ids"]["items"] == {"type": "string"}


def test_prompt_names_exact_output_keys_and_keeps_citation_convention():
    convention = PROMPT[PROMPT.index("Citation convention:") :]
    assert convention.startswith("Citation convention: quote is the complete text")
    assert convention.endswith("within a field, by first mention.")
    for key in ("schema", "diagnoses", "verdict", "opspilot-judge-1"):
        assert f'"{key}"' in PROMPT
    for field in dataclasses.fields(JudgedDiagnosis):
        assert f'"{field.name}":' in PROMPT
    assert "markdown" in PROMPT and "fences" in PROMPT
    assert "keyed by exact location" in PROMPT


def _quoted_payload(claims=None):
    payload = dict(safe_input(judge_input()))
    report = json.loads(payload["report_raw"])
    report["summary"] = 'cart "EmptyCart" failed and caused checkout failures.'
    base = report["claims"][0]
    report["claims"] = claims or [
        {**base, "text": 'cart "EmptyCart" failed and caused checkout failures.'},
        {**base, "kind": "hypothesis", "text": 'checkout "PlaceOrder" may retry.'},
    ]
    report["gaps"] = ["Broader impact remains unmeasured.", 'Is "redis" involved?']
    payload["report_raw"] = json.dumps(report)
    return payload, report


@pytest.mark.parametrize("provider", PROVIDERS)
def test_user_message_appends_location_labelled_report_fields(configs, provider):
    """judge-adapters-5: payload unchanged, then a block keyed by exact location."""
    payload, report = _quoted_payload()
    body = json.loads(_ADAPTERS[provider].build(configs[provider], payload))
    content = body["messages"][1]["content"]
    sent, end = json.JSONDecoder().raw_decode(content)
    # The whitelist payload (and so every input hash) is byte-identical.
    assert content[:end] == canonical_bytes(payload).decode("utf-8")
    assert sent == payload
    block = content[end:]
    assert block.startswith(REPORT_FIELDS_HEADER)
    assert block.endswith(REPORT_FIELDS_FOOTER)
    fields = json.loads(block[len(REPORT_FIELDS_HEADER) : -len(REPORT_FIELDS_FOOTER)])
    assert list(fields) == [
        "summary",
        "claims[0]",
        "claims[1]",
        "gaps[0]",
        "gaps[1]",
        "next_steps[0]",
    ]
    parsed = parse_report(payload["report_raw"], finish_reason="stop")[0]
    for location, value in fields.items():
        text, claim_index = _location_text(parsed, location)
        if claim_index is None:
            assert value == text
        else:
            assert value == {
                "text": text,
                "evidence_ids": report["claims"][claim_index]["evidence_ids"],
            }
    # Quotes are single-escaped JSON in the block, not the doubly escaped
    # report_raw string form.
    assert '\\\\\\"EmptyCart\\\\\\"' in content[:end]
    assert '"summary": "cart \\"EmptyCart\\" failed' in block
    assert "\\\\" not in block


def test_report_fields_block_stays_under_request_limit(configs):
    """A large realistic report (60 claims x 2 KiB) still fits 8 MiB with the block."""
    payload, report = _quoted_payload()
    base = report["claims"][0]
    big = [{**base, "text": f'claim {i} "quoted" ' + "x" * 2048} for i in range(60)]
    payload, _ = _quoted_payload(claims=big)
    for provider in PROVIDERS:
        body = _ADAPTERS[provider].build(configs[provider], payload)
        assert len(body) < configs[provider].max_request_bytes // 8


def test_adapter_version_is_judge_adapters_5(configs):
    """judge-adapters-5 (enforced JSON output, cheaper candidates); older refused."""
    for config in configs.values():
        assert config.adapter_version == "judge-adapters-5"
        check_config(config)
    for old in (
        "judge-adapters-1",
        "judge-adapters-2",
        "judge-adapters-3",
        "judge-adapters-4",
    ):
        stale = replace(configs["zai"], adapter_version=old)
        with pytest.raises(JudgeError, match="CONFIG_INVALID"):
            check_config(stale)
    wire = Wire(sse_body(answer_chunks()))
    observation = run(configs, wire, adapter_version="judge-adapters-2")
    assert observation.error_code == "CONFIG_INVALID"
    assert wire.requests == []


# ------------------------------------------------------- S15 total deadline


def test_keepalive_bytes_do_not_extend_total_deadline(configs):
    trickle = Pieces([b": keep-alive\n\n"] * 200, delay=0.01)
    started = time.monotonic()
    observation = run(configs, Wire(trickle), timeout_seconds=0.3)
    assert_failed(observation, "timeout", "JUDGE_TIMEOUT")
    assert time.monotonic() - started < 1.5


# ------------------------------------------- review regressions (astra P1s)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("ident", ["missing", "", 7, None])
def test_id_must_be_non_empty_string_in_every_chunk(configs, provider, ident):
    """S14 (lead 2026-10-20): consistently absent/non-string id is not identity."""
    events = answer_chunks(provider)
    for event in events:
        if ident == "missing":
            del event["id"]
        else:
            event["id"] = ident
    observation = run(configs, Wire(sse_body(events)), provider)
    assert_failed(observation, *IDENTITY_CODE[provider])


def test_bytes_after_done_in_same_chunk_are_not_counted(configs):
    """S8: reading stops at [DONE]; trailing bytes in that chunk are ignored."""
    body = sse_body(answer_chunks()) + b"x" * (33 * MIB)
    observation = run(configs, Wire(body))  # one network chunk
    assert_ok(observation)
