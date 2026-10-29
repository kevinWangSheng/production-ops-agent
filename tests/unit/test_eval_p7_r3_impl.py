"""P7 r3 driver changes (P7-D20 raw bodies, P7-D21 tuning debug); fake transports."""

from __future__ import annotations

import asyncio
import gzip
import json
import sys
import zlib
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "acceptance"))

from eval_p7_support import DriverHarness, snapshot  # noqa: E402

from opspilot.evaluation.judge import (  # noqa: E402
    HTTPJudgeTransport,
    JudgeError,
    JudgeHTTPResponse,
    judge_once,
)
from scripts import eval_p7  # noqa: E402

# Captured before any harness patches send (the harness fakes every call).
ORIGINAL_SEND = HTTPJudgeTransport.send


@pytest.fixture
def harness(tmp_path, monkeypatch, capsys):
    return DriverHarness(tmp_path, monkeypatch, capsys, eval_p7).init()


def debug(h, session, *args, **kwargs):
    h.costs(session)
    return h.cli("debug", "--session", session, "--judge-env", h.env, *args, **kwargs)


def rows(h):
    return [
        json.loads(line)
        for line in (h.private / "exposure.jsonl").read_bytes().splitlines()
    ]


def test_tuning_debug_resends_formal_validation_items_but_formal_refuses(harness):
    h = harness.ready()
    h.formal()
    validation = {i.item_id for i in h.items if i.split == "validation"}
    families = {i.source_family for i in h.items if i.split == "validation"}
    with h.mp.context() as m:
        # The judge prompt moved on from the exported packet's convention.
        m.setattr(eval_p7, "judge", SimpleNamespace(PROMPT="changed prompt"))
        debug(h, "plain-debug", code=3, reason="JUDGE_PROMPT_CHANGED")
        h.calls.clear()
        debug(h, "tuning-debug", "--tuning")
    assert len(h.calls) == 36 * 3
    records = [
        json.loads(p.read_bytes()) for p in (h.ws / "debug/tuning-debug").glob("*.json")
    ]
    assert validation <= {r["item_id"] for r in records}
    row = rows(h)[-1]
    assert row["kind"] == "debug" and row["tuning"] is True
    assert families <= set(row["debug_families"])
    h.costs("canary-formal-2")
    h.calls.clear()
    before = snapshot(h.root)
    h.cli(
        "formal",
        "--run",
        "canary-formal-2",
        "--pack-hash",
        h.pack_hash,
        "--judge-env",
        h.env,
        code=3,
        reason="VALIDATION_EXPOSED",
    )
    assert snapshot(h.root) == before and not h.calls


def test_tuning_debug_refuses_validation_items_no_formal_run_exposed(harness):
    h = harness.seed().export().annotations()
    item = next(i.item_id for i in h.items if i.split == "validation")
    h.costs("tuning-debug")
    before = snapshot(h.root)
    h.cli(
        "debug",
        "--session",
        "tuning-debug",
        "--judge-env",
        h.env,
        "--tuning",
        "--item",
        item,
        code=3,
        reason="VALIDATION_EXPOSURE_FORBIDDEN",
    )
    assert snapshot(h.root) == before and not h.calls
    h.cli("debug", "--session", "tuning-debug", "--judge-env", h.env, "--tuning")
    assert len(h.calls) == 12 * 3


def test_rejected_answers_keep_their_raw_response_body(harness):
    h = harness.seed().export()
    answer = HTTPJudgeTransport.send

    def send(transport, **kwargs):
        response = answer(transport, **kwargs)
        if "z.ai" in kwargs["endpoint"]:
            return JudgeHTTPResponse(200, (b"not a completion",))
        if "openai" in kwargs["endpoint"]:
            return JudgeHTTPResponse(503, (b'{"error": "busy"}',))
        return response

    h.mp.setattr(HTTPJudgeTransport, "send", send)
    debug(h, "raw-debug")
    directory = h.ws / "debug/raw-debug"
    assert (directory / "zai-001.raw").read_bytes() == b"not a completion"
    assert (directory / "openai-001.raw").read_bytes() == b'{"error": "busy"}'
    assert not list(directory.glob("deepseek-*.raw"))
    assert len(list(directory.glob("*.raw"))) == 24
    record = json.loads((directory / "openai-001.json").read_bytes())
    assert record["observation"]["error_code"] == "JUDGE_HTTP_ERROR"
    # Raw bodies stay out of the observation-based summaries.
    assert (
        h.cli("cost", "--session", "raw-debug")["cost"]["providers"]["zai"]["calls"]
        == 12
    )


def test_formal_raw_write_failure_does_not_fail_a_completed_run(harness):
    h = harness.ready()

    def broken(*_args):
        raise eval_p7.P7Error("ARTIFACT_UNWRITABLE")

    h.mp.setattr(eval_p7, "_formal_raw_outputs", broken)
    result = h.formal()
    assert result["raw_output_error"] == "ARTIFACT_UNWRITABLE"
    assert result["status"] == "selected"


def _wire(content_type, body, **headers):
    def handler(_request):
        return httpx2.Response(
            200, headers={"content-type": content_type, **headers}, content=body
        )

    return httpx2.MockTransport(handler)


def _send(capture, limit=1000):
    return capture.send(
        endpoint="https://judge.invalid/v1/chat/completions",
        headers={},
        body=b"{}",
        timeout_seconds=5.0,
        max_response_bytes=limit,
    )


@pytest.mark.parametrize(
    ("content_type", "body", "code"),
    [
        ("application/json", b'{"not": "a stream"}', "JUDGE_OUTPUT_INVALID"),
        ("text/event-stream", b"data: {broken\n\n", "JUDGE_OUTPUT_INVALID"),
        ("text/event-stream", b": keepalive\n\n", "JUDGE_TRANSPORT_ERROR"),
    ],
)
def test_a_body_the_http_transport_rejects_is_still_captured(content_type, body, code):
    capture = eval_p7._CapturingTransport(
        HTTPJudgeTransport(transport=_wire(content_type, body))
    )
    with pytest.raises(JudgeError) as caught:
        _send(capture)
    assert caught.value.code == code
    assert bytes(capture.body) == body and capture.sends[-1].body == body


def test_captured_rejected_body_is_bounded_and_decoded():
    body = b"x" * 50
    capture = eval_p7._CapturingTransport(
        HTTPJudgeTransport(
            transport=_wire(
                "application/json", gzip.compress(body), **{"content-encoding": "gzip"}
            )
        )
    )
    with pytest.raises(JudgeError):
        _send(capture, limit=20)
    assert bytes(capture.body) == body[:20]


def test_debug_keeps_the_raw_body_of_a_stream_the_transport_rejected(harness):
    h = harness.seed().export()
    patched = HTTPJudgeTransport.send

    def send(transport, **kwargs):
        if "z.ai" in kwargs["endpoint"]:
            return ORIGINAL_SEND(transport, **kwargs)
        return patched(transport, **kwargs)

    h.mp.setattr(HTTPJudgeTransport, "send", send)
    h.mp.setattr(
        httpx2,
        "AsyncHTTPTransport",
        lambda **_: _wire("text/event-stream", b"data: {broken\n\n"),
    )
    debug(h, "wire-debug")
    directory = h.ws / "debug/wire-debug"
    assert (directory / "zai-001.raw").read_bytes() == b"data: {broken\n\n"
    record = json.loads((directory / "zai-001.json").read_bytes())
    assert record["observation"]["error_code"] == "JUDGE_OUTPUT_INVALID"
    assert len(list(directory.glob("zai-*.raw"))) == 12


class _BrokenBody(httpx2.AsyncByteStream):
    def __init__(self, failure):
        self.failure = failure

    async def __aiter__(self):
        yield b"partial"
        if self.failure == "stall":
            await asyncio.sleep(60)
        raise httpx2.ReadError("connection reset")


@pytest.mark.parametrize("failure", ["raise", "stall"])
def test_capturing_a_wrong_content_type_body_never_changes_the_outcome(
    failure, monkeypatch
):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_eval_b3_impl import configs, make_input

    monkeypatch.setattr(eval_p7, "_CAPTURE_SECONDS", 0.2)
    sends = []

    def handler(request):
        sends.append(request)
        return httpx2.Response(
            200,
            headers={"content-type": "application/json"},
            stream=_BrokenBody(failure),
        )

    capture = eval_p7._CapturingTransport(
        HTTPJudgeTransport(transport=httpx2.MockTransport(handler))
    )
    request, _, _ = make_input()
    observation = judge_once(
        request, config=configs()["zai"], calibration=None, transport=capture
    )
    assert observation.error_code == "JUDGE_OUTPUT_INVALID"
    assert observation.attempts == 1 and len(sends) == 1
    assert observation.elapsed_seconds < 5
    assert bytes(capture.body) == b"partial"


def test_captured_raw_deflate_body_is_decoded():
    body = b"y" * 50
    packer = zlib.compressobj(wbits=-15)
    raw = packer.compress(body) + packer.flush()
    capture = eval_p7._CapturingTransport(
        HTTPJudgeTransport(
            transport=_wire("application/json", raw, **{"content-encoding": "deflate"})
        )
    )
    with pytest.raises(JudgeError):
        _send(capture, limit=20)
    assert bytes(capture.body) == body[:20]


def _empty_zai_refused_openai(h):
    answer = HTTPJudgeTransport.send

    def send(transport, **kwargs):
        if "z.ai" in kwargs["endpoint"]:
            return JudgeHTTPResponse(200, ())
        if "openai" in kwargs["endpoint"]:
            raise httpx2.ConnectError("connection refused")
        return answer(transport, **kwargs)

    h.mp.setattr(HTTPJudgeTransport, "send", send)


def test_debug_keeps_an_empty_rejected_response_but_not_a_refused_connection(
    harness,
):
    h = harness.seed().export()
    _empty_zai_refused_openai(h)
    debug(h, "empty-debug")
    directory = h.ws / "debug/empty-debug"
    assert (directory / "zai-001.raw").read_bytes() == b""
    record = json.loads((directory / "openai-001.json").read_bytes())
    assert record["observation"]["error_code"] == "JUDGE_TRANSPORT_ERROR"
    assert not list(directory.glob("openai-*.raw"))


def test_formal_keeps_an_empty_rejected_response_but_not_a_refused_connection(
    harness,
):
    h = harness.ready()
    _empty_zai_refused_openai(h)
    assert h.formal(code=1)["status"] == "no_baseline"
    directory = h.ws / "calibration/canary-formal"
    assert (directory / "zai-001.raw").read_bytes() == b""
    assert (directory / "openai-001.json").exists()
    assert not list(directory.glob("openai-*.raw"))
