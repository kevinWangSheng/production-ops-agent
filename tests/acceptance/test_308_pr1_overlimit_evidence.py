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
    ControlSnapshot,
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

# PR1 froze the refusal bytes; #308 PR2 deliberately changes the model view
# (evidence_id, U5 summary fields, usage text) and projection v9, so the bytes
# are no longer pinned.  The keys PR1 guaranteed stay.
BASELINE_REFUSAL_KEYS = frozenset(
    {
        "operation_id",
        "trust",
        "status",
        "citable_as_fact",
        "reason",
        "source_contact",
        "tool",
        "target_id",
        "window",
        "requested_at",
        "content",
        "query",
        "max_view_tokens",
        "view_tokens",
        "message",
    }
)
SPILLED_REFUSAL_KEYS = BASELINE_REFUSAL_KEYS | frozenset(
    {
        "evidence_id",
        "adopted",
        "observed_at",
        "data_as_of",
        "source_start_at",
        "source_end_at",
        "result_count",
        "incomplete",
        "source",
        "tool_version",
        "projection_revision",
        "dispatch_started_at",
        "freshness_seconds",
    }
)

# Frozen from main@aa57f9a + contract commit 5da7be9.  This is the existing
# post-fetch control-generation refusal; PR1 must keep this model-facing view
# unchanged while retaining the fetched result only as history.
BASELINE_CONTROL_GENERATION_REFUSAL_VIEW = (
    b'{"citable_as_fact":false,"content":null,"operation_id":"step-1-t0",'
    b'"reason":"CONTROL_GENERATION_CHANGED","requested_at":"2026-09-14T01:05:00+00:00",'
    b'"source_contact":"confirmed","status":"denied","target_id":"checkout-prod",'
    b'"tool":"metrics.range_query","trust":"gateway","window":{"end":"2026-09-14T00:10:00+00:00",'
    b'"start":"2026-09-14T00:00:00+00:00"}}'
)

# Unchanged by #308 PR2. F3 PR-2 (#311 + #310) contract change: #310 moves prompt_revision, so it
# is now compared for inequality against this value; the context policy stays.
UNCHANGED_VERSIONS = {
    "prompt_revision": "prompt-replay-candidate-dd9018031487",
    "context_policy_revision": "ctx-ctx-policy-v1-a30e65ea52f4",
}
OLD_TOOL_SCHEMA_REVISION = "otel-demo-5270fd8c543f"  # main after #318 (c38be65)
OLD_PROJECTION_REVISION = "m1-01-tool-view-v8"
# #308 PR2 bumps each once (contract R10). F3 PR-2 (#311 + #310) contract change: #311 bumps the
# projection to v10 and moves the tool schema revision again; the #308 PR2
# tool schema value is kept as the previous freeze (inequality only).
NEW_PROJECTION_REVISION = "m1-01-tool-view-v10"
NEW_TOOL_SCHEMA_REVISION = "otel-demo-2a944edd110a"
# U5 fields every refusal view carries after PR2 (the usage-text key is not
# named by the contract, so it is not listed).
U5_REFUSAL_KEYS = frozenset({"total_rows", "row_unit", "suggested_limit"})

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


def test_pr1_view_overlimit_registers_non_adopted_record_and_pr2_attaches_it():
    sink = _CountingSink()
    executor, transport = _executor(sink=sink)

    outcome = _overlimit_outcome(executor, transport)

    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert len(sink.records) == 1
    record = sink.records[0]
    # R1 (PR2): the model view is the stored view and the record is attached.
    assert outcome.evidence == record
    assert canonical(outcome.model_view) == canonical(record.view)
    assert record.operation.run_id == RUN_ID
    assert record.status == "error"
    assert record.adopted is False
    assert record.projection_revision == NEW_PROJECTION_REVISION
    assert record.raw == _redacted_body()
    assert record.raw_sha256 == hashlib.sha256(record.raw).hexdigest()
    assert record.view_sha256 == canonical_hash(record.view)
    view = record.view
    # R12 provenance keys plus the U5 summary and usage keys (PR2); extra
    # summary keys beyond these are not forbidden here, row content is.
    assert SPILLED_REFUSAL_KEYS | U5_REFUSAL_KEYS <= set(view)
    for key in (
        "operation_id",
        "trust",
        "status",
        "citable_as_fact",
        "reason",
        "source_contact",
        "tool",
        "target_id",
        "window",
        "requested_at",
        "query",
        "max_view_tokens",
    ):
        assert view[key] == outcome.model_view[key]
    assert view["content"] is None
    assert view["total_rows"] == len(_rows())
    assert view["evidence_id"] == record.evidence_id
    assert view["adopted"] is False
    assert view["observed_at"] == outcome.operation.finished_at.isoformat()
    assert view["data_as_of"] is None
    assert view["source_start_at"] is None
    assert view["source_end_at"] is None
    assert view["result_count"] == len(_rows())
    assert view["incomplete"] is False
    assert view["source"] == "prometheus"
    assert view["tool_version"] == "v1"
    assert view["projection_revision"] == NEW_PROJECTION_REVISION
    assert view["dispatch_started_at"] == outcome.operation.started_at.isoformat()
    assert view["freshness_seconds"] is None
    assert b"password=xyz" not in record.raw
    view_text = canonical(view)
    assert "password=xyz" not in view_text
    assert '"series":' not in view_text  # no row key (the summary says "series")
    assert "xxxxxxxx" not in view_text


def test_pr1_byte_limit_refusal_does_not_register_a_row():
    sink = _CountingSink()
    executor, transport = _executor(sink=sink, max_result_bytes=32)
    transport.response = TransportResponse(body=b"x" * 64)

    outcome = executor.execute(_request())

    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert outcome.evidence is None
    assert sink.records == []


def test_pr1_seventeenth_overlimit_result_is_not_stored():
    sink = _CountingSink()
    executor, transport = _executor(sink=sink)

    outcomes = [
        _overlimit_outcome(executor, transport, index=index) for index in range(17)
    ]

    assert all(
        (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
        for outcome in outcomes
    )
    assert all(outcome.evidence is not None for outcome in outcomes[:16])
    assert outcomes[16].evidence is None
    assert outcomes[16].model_view["stored"] is False  # U2, PR2
    assert len(sink.records) == 16
    assert sink.count_spilled(RUN_ID) == (
        16,
        sum(len(record.raw) for record in sink.records),
    )


def test_pr1_cumulative_raw_bytes_stop_storage_after_the_16_mib_boundary():
    sink = _CountingSink()
    executor, transport = _executor(sink=sink, max_result_bytes=2 * 1024 * 1024)
    exact_boundary_payload = _large_result_body(2 * 1024 * 1024)
    next_payload = _large_result_body(30_000)

    stored = [
        _large_overlimit_outcome(
            executor, transport, exact_boundary_payload, index=index
        )
        for index in range(8)
    ]
    before = sink.count_spilled(RUN_ID)
    over_boundary = _large_overlimit_outcome(executor, transport, next_payload, index=8)

    assert all(
        (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
        for outcome in (*stored, over_boundary)
    )
    assert before == (8, 16 * 1024 * 1024)
    assert before[0] < 16
    assert over_boundary.model_view["stored"] is False  # U2, PR2
    assert over_boundary.evidence is None
    assert len(sink.records) == 8
    assert before == sink.count_spilled(RUN_ID)


def test_pr1_unavailable_spill_count_keeps_refusal_and_does_not_store():
    sink = _UnanswerableSink()
    executor, transport = _executor(sink=sink)

    outcome = _overlimit_outcome(executor, transport)

    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert outcome.model_view["adopted"] is False
    assert outcome.model_view["citable_as_fact"] is False
    assert outcome.model_view.get("content") is None
    assert outcome.evidence is None
    assert sink.records == []


def test_pr1_post_fetch_generation_change_keeps_history_without_a_spill_record():
    sink = _CountingSink()
    moved = ControlSnapshot(
        control_generation=8,
        global_suspension_generation=0,
        target_suspension_generation=0,
        suspended=False,
    )
    control = FixedControl(later=moved, later_after=2)
    executor, transport = _executor(sink=sink, control=control)

    outcome = _overlimit_outcome(executor, transport)

    assert (outcome.status, outcome.reason) == (
        "denied",
        "CONTROL_GENERATION_CHANGED",
    )
    assert outcome.source_contact == "confirmed"
    assert outcome.operation.sent is True
    assert transport.called is True
    assert canonical(outcome.model_view).encode() == (
        BASELINE_CONTROL_GENERATION_REFUSAL_VIEW
    )
    assert len(sink.records) == 1
    history = sink.records[0]
    assert outcome.evidence is history
    assert history.status == "denied"
    assert history.adopted is False
    assert history.raw == _redacted_body()
    assert history.view["content"] is None
    assert sink.count_spilled(RUN_ID) == (0, 0)


def test_pr1_control_failure_keeps_the_existing_denied_path():
    sink = _CountingSink()
    executor, transport = _executor(sink=sink, control=FixedControl(suspended=True))

    outcome = _overlimit_outcome(executor, transport)

    assert (outcome.status, outcome.reason) == ("denied", "SUSPENDED")
    assert outcome.operation.sent is False
    assert outcome.evidence is None
    assert sink.records == []
    assert transport.called is False


def test_pr2_bumps_tool_schema_and_projection_once_and_keeps_the_rest():
    assert otel_demo.TOOL_SCHEMA_REVISION != OLD_TOOL_SCHEMA_REVISION
    assert otel_demo.TOOL_SCHEMA_REVISION != NEW_TOOL_SCHEMA_REVISION
    assert otel_demo.TOOL_SCHEMA_REVISION == otel_demo._tool_schema_revision()
    assert PROJECTION_REVISION != OLD_PROJECTION_REVISION
    assert PROJECTION_REVISION == NEW_PROJECTION_REVISION
    assert (
        investigation_versions()["prompt_revision"]
        != UNCHANGED_VERSIONS["prompt_revision"]
    )
    assert (
        investigation_versions()["context_policy_revision"]
        == UNCHANGED_VERSIONS["context_policy_revision"]
    )
    assert context_policy_versions() == {
        "context_policy_revision": UNCHANGED_VERSIONS["context_policy_revision"]
    }
    assert otel_demo.otel_demo_versions() == {
        **investigation_versions(),
        "tool_schema_revision": otel_demo.TOOL_SCHEMA_REVISION,
    }


def test_pr1_transport_result_too_large_exception_stays_unstored():
    sink = _CountingSink()
    executor, transport = _executor(sink=sink)
    transport.error = TransportResultTooLarge()

    outcome = executor.execute(_request())

    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert outcome.evidence is None
    assert sink.records == []
