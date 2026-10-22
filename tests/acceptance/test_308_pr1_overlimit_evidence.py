"""Independent PR1 acceptance tests for #308.

The contract keeps the model-facing refusal unchanged while persisting a
redacted, non-adopted evidence row for a view that is over 25,000 tokens.
These tests use the public executor, registration, outcome and evidence
interfaces.  PostgreSQL row assertions live in the companion integration
module; no real model or backend is contacted here.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import datetime, timezone

from opspilot.investigation.context import context_policy_versions
from opspilot.investigation.loop import investigation_versions
from opspilot.redaction import REDACTED_CREDENTIAL
from opspilot.tools import (
    ReadOnlyToolExecutor,
    ToolRequest,
    TransportResponse,
    TransportResultTooLarge,
    otel_demo,
)
from opspilot.tools.outcomes import PROJECTION_REVISION
from opspilot.tools.registry import (
    TargetRegistry,
    ToolRegistry,
    canonical,
    canonical_hash,
)
from tests.m1_tool_support import (
    FakeTransport,
    FixedControl,
    RecordingLedger,
    body,
    registration,
    scope,
    target,
)

# Frozen from the model view produced by main@aa57f9a + contract commit
# 5da7be9 (2026-10-22).  The fixture fixes the clock and request index, so the
# audit fields are deterministic and this is a byte-for-byte oracle for PR1's
# "model view unchanged" requirement.
BASELINE_REFUSAL_VIEW = (
    b'{"citable_as_fact":false,"content":null,"max_view_tokens":25000,'
    b'"message":"The tool call result is too large to return: 38695/25000 '
    b"tokens.\\nTry to repeat the query but proactively narrow down the result "
    b"(a narrower time window, a filter, or a smaller limit) so that the tool "
    b'answer fits within the allowed number of tokens.","operation_id":"step-1-t0",'
    b'"query":{"expr":"rate(http_errors[5m])"},"reason":"RESULT_TOO_LARGE",'
    b'"requested_at":"2026-09-14T01:05:00+00:00","source_contact":"confirmed",'
    b'"status":"error","target_id":"checkout-prod","tool":"metrics.range_query",'
    b'"trust":"gateway","view_tokens":38695,"window":{"end":"2026-09-14T00:10:00+00:00",'
    b'"start":"2026-09-14T00:00:00+00:00"}}'
)

OLD_OTEL_VERSIONS = {
    "prompt_revision": "prompt-replay-candidate-dd9018031487",
    "context_policy_revision": "ctx-ctx-policy-v1-a30e65ea52f4",
    "tool_schema_revision": "otel-demo-c7737da709ee",
}
OLD_PROJECTION_REVISION = "m1-01-tool-view-v8"

RUN_ID = "run-308-pr1"


def _byte_counter(text: str) -> int:
    return len(text.encode("utf-8"))


def _rows() -> list[dict[str, object]]:
    return [
        {
            "series": index,
            "message": "password=xyz " + ("x" * 1200),
        }
        for index in range(30)
    ]


def _request(index: int = 0) -> ToolRequest:
    return ToolRequest(
        step_id="step-1",
        tool_index=index,
        tool_name="metrics.range_query",
        target_ref="checkout-prod",
        params={"expr": "rate(http_errors[5m])"},
        window={
            "start": "2026-09-14T00:00:00+00:00",
            "end": "2026-09-14T00:10:00+00:00",
        },
    )


def _executor(*, sink, control=None, max_result_bytes=1_000_000):
    tools = ToolRegistry(
        [
            dataclasses.replace(
                registration(),
                max_view_tokens=25_000,
                max_result_bytes=max_result_bytes,
            )
        ]
    )
    targets = TargetRegistry([target()])
    transport = FakeTransport()
    executor = ReadOnlyToolExecutor(
        scope=scope(
            targets,
            tool_registry=tools,
            run_id=RUN_ID,
        ),
        tools=tools,
        targets=targets,
        transport=transport,
        evidence=sink,
        control=control or FixedControl(),
        clock=_FixedClock(),
        ledger=RecordingLedger(),
        token_counter=_byte_counter,
    )
    return executor, transport


class _FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 9, 14, 1, 5, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return 1_000.0


class _CountingSink:
    """Public EvidenceSink seam plus the PR1 per-Run spill-count seam."""

    def __init__(self) -> None:
        self.records = []

    def register(self, record):
        self.records.append(record)
        return record.evidence_id

    def count_spilled(self, run_id: str) -> tuple[int, int]:
        records = [
            record
            for record in self.records
            if record.operation.run_id == run_id
            and record.status == "error"
            and record.adopted is False
        ]
        return len(records), sum(len(record.raw) for record in records)


class _UnanswerableSink(_CountingSink):
    def count_spilled(self, run_id: str) -> tuple[int, int]:
        raise RuntimeError("spill count unavailable")


def _redacted_body() -> bytes:
    expected = {
        "data": {
            "result": [
                {
                    "message": f"password={REDACTED_CREDENTIAL} " + ("x" * 1200),
                    "series": index,
                }
                for index in range(30)
            ]
        }
    }
    return json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()


def _baseline_for_index(index: int) -> bytes:
    operation_id = BASELINE_REFUSAL_VIEW.replace(
        b'"operation_id":"step-1-t0"',
        f'"operation_id":"step-1-t{index}"'.encode(),
    )
    # The adopted view used to calculate the refusal count carries the
    # operation id twice (as ``evidence_id`` and ``operation_id``).
    tokens = 38_695 + 2 * (len(str(index)) - 1)
    return operation_id.replace(b"38695", str(tokens).encode())


def _overlimit_outcome(executor, transport, *, index: int = 0):
    transport.response = TransportResponse(body=body(_rows()))
    return executor.execute(_request(index))


def _large_result_body(target_bytes: int = 995_000) -> bytes:
    prefix = b'{"data":{"result":[{"series":0,"message":"'
    suffix = b'"}]}}'
    return prefix + (b"x" * (target_bytes - len(prefix) - len(suffix))) + suffix


def _large_overlimit_outcome(executor, transport, payload: bytes, *, index: int):
    transport.response = TransportResponse(body=payload)
    return executor.execute(_request(index))


def test_pr1_view_overlimit_keeps_main_model_bytes_and_registers_non_adopted_record():
    sink = _CountingSink()
    executor, transport = _executor(sink=sink)

    outcome = _overlimit_outcome(executor, transport)

    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert canonical(outcome.model_view).encode() == _baseline_for_index(0)
    # R1: registration is through the sink; the refusal is not attached to the
    # model outcome and therefore cannot alter eval/replay model bytes.
    assert outcome.evidence is None
    assert len(sink.records) == 1
    record = sink.records[0]
    assert record.operation.run_id == RUN_ID
    assert record.status == "error"
    assert record.adopted is False
    assert record.projection_revision == OLD_PROJECTION_REVISION
    assert record.raw == _redacted_body()
    assert record.raw_sha256 == hashlib.sha256(record.raw).hexdigest()
    assert record.view_sha256 == canonical_hash(record.view)


def test_pr1_byte_limit_refusal_does_not_register_a_row():
    sink = _CountingSink()
    executor, transport = _executor(sink=sink, max_result_bytes=32)
    transport.response = TransportResponse(body=b"x" * 64)

    outcome = executor.execute(_request())

    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert outcome.evidence is None
    assert sink.records == []


def test_pr1_seventeenth_overlimit_result_is_not_stored_and_view_is_unchanged():
    sink = _CountingSink()
    executor, transport = _executor(sink=sink)

    outcomes = [
        _overlimit_outcome(executor, transport, index=index) for index in range(17)
    ]

    assert all(
        (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
        for outcome in outcomes
    )
    assert all(
        canonical(outcome.model_view).encode() == _baseline_for_index(index)
        for index, outcome in enumerate(outcomes)
    )
    assert len(sink.records) == 16
    assert sink.count_spilled(RUN_ID) == (
        16,
        sum(len(record.raw) for record in sink.records),
    )


def test_pr1_cumulative_raw_bytes_stop_storage_after_the_16_mib_boundary():
    sink = _CountingSink()
    executor, transport = _executor(sink=sink)
    payload = _large_result_body()
    assert len(payload) < 1_000_000

    stored = [
        _large_overlimit_outcome(executor, transport, payload, index=index)
        for index in range(16)
    ]
    before = sink.count_spilled(RUN_ID)
    seventeenth = _large_overlimit_outcome(executor, transport, payload, index=16)

    baseline_sink = _CountingSink()
    baseline_executor, baseline_transport = _executor(sink=baseline_sink)
    baseline = _large_overlimit_outcome(
        baseline_executor, baseline_transport, payload, index=16
    )
    assert all(
        (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
        for outcome in (*stored, seventeenth, baseline)
    )
    assert canonical(seventeenth.model_view) == canonical(baseline.model_view)
    assert len(sink.records) == 16
    assert before == sink.count_spilled(RUN_ID)
    assert before[1] * 17 > 16 * 1024 * 1024


def test_pr1_unavailable_spill_count_keeps_refusal_and_does_not_store():
    sink = _UnanswerableSink()
    executor, transport = _executor(sink=sink)

    outcome = _overlimit_outcome(executor, transport)

    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert canonical(outcome.model_view).encode() == _baseline_for_index(0)
    assert outcome.evidence is None
    assert sink.records == []


def test_pr1_control_failure_keeps_the_existing_denied_path():
    sink = _CountingSink()
    executor, transport = _executor(sink=sink, control=FixedControl(suspended=True))

    outcome = _overlimit_outcome(executor, transport)

    assert (outcome.status, outcome.reason) == ("denied", "SUSPENDED")
    assert outcome.operation.sent is False
    assert outcome.evidence is None
    assert sink.records == []
    assert transport.called is False


def test_pr1_does_not_bump_any_model_or_projection_revision():
    assert otel_demo.TOOL_SCHEMA_REVISION == OLD_OTEL_VERSIONS["tool_schema_revision"]
    assert PROJECTION_REVISION == OLD_PROJECTION_REVISION
    assert investigation_versions() == {
        "prompt_revision": OLD_OTEL_VERSIONS["prompt_revision"],
        "context_policy_revision": OLD_OTEL_VERSIONS["context_policy_revision"],
    }
    assert context_policy_versions() == {
        "context_policy_revision": OLD_OTEL_VERSIONS["context_policy_revision"]
    }
    assert otel_demo.otel_demo_versions() == OLD_OTEL_VERSIONS


def test_pr1_transport_result_too_large_exception_stays_unstored():
    sink = _CountingSink()
    executor, transport = _executor(sink=sink)
    transport.error = TransportResultTooLarge()

    outcome = executor.execute(_request())

    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert outcome.evidence is None
    assert sink.records == []
