"""#308 PR1 implementer tests: an over-limit view is kept as un-adopted evidence.

Contract: ``docs/tasks/2026-10-22-308-overlimit-evidence-contract.md`` (U1-U3,
R1). The model view stays byte-for-byte the refusal it was before; the redacted
raw body and the refusal view (plus ``evidence_id``) are registered through the
sink only, at most 16 records or 16 MiB per Run. The PostgreSQL side (worker
grants, ``count_spilled`` SQL) is in
``tests/integration/test_308_overlimit_evidence_impl_postgres.py``.
"""

from __future__ import annotations

import dataclasses
import json
from hashlib import sha256

import pytest

import opspilot.tools.executor as executor_module
from opspilot.redaction import redact_payload
from opspilot.tools import (
    ControlSnapshot,
    ReadOnlyToolExecutor,
    TargetRegistry,
    ToolRegistry,
    TransportResponse,
)
from opspilot.tools.registry import canonical
from opspilot.web.evidence import MemoryEvidenceStore
from tests.m1_tool_support import (
    FakeClock,
    FakeTransport,
    FixedControl,
    RecordingLedger,
    RecordingSink,
    body,
    registration,
    request,
    scope,
    target,
)

ROWS = [
    {"metric": {"msg": f"login password=xyz row {index}"}, "value": index}
    for index in range(12)
]
CAP = 64  # tokens; the whole view of ``ROWS`` is far larger


def bytes_counter(text: str) -> int:
    return len(text.encode("utf-8"))


def _build(sink, *, reg=None, control=None, run_id="run-9"):
    clock = FakeClock()
    reg = reg or dataclasses.replace(
        registration(), max_view_tokens=CAP, max_result_bytes=1 << 20
    )
    tools = ToolRegistry([reg])
    targets = TargetRegistry([target()])
    transport = FakeTransport(clock=clock)
    executor = ReadOnlyToolExecutor(
        scope=scope(targets, tool_registry=tools, run_id=run_id),
        tools=tools,
        targets=targets,
        transport=transport,
        evidence=sink,
        control=control if control is not None else FixedControl(),
        clock=clock,
        ledger=RecordingLedger(),
        token_counter=bytes_counter,
    )
    return executor, transport


def _run(sink, payload, **kwargs):
    executor, transport = _build(sink, **kwargs)
    transport.response = TransportResponse(body=payload)
    return executor.execute(request())


def test_over_limit_view_is_stored_unadopted_and_model_view_is_unchanged():
    payload = body(ROWS)
    baseline = _run(RecordingSink(), payload)  # a sink that cannot count: pre-#308
    store = MemoryEvidenceStore()
    outcome = _run(store, payload)

    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert canonical(outcome.model_view) == canonical(baseline.model_view)
    assert "evidence_id" not in outcome.model_view
    assert outcome.evidence is None  # R1: sink only

    count, size = store.count_spilled("run-9")
    assert count == 1
    (stored,) = store._records.values()
    assert stored.run_id == "run-9"
    assert stored.adopted is False and stored.status == "error"
    assert stored.hashes_verified
    assert stored.raw == canonical(redact_payload(json.loads(payload))).encode("utf-8")
    assert b"xyz" not in stored.raw and size == len(stored.raw)
    assert stored.raw_sha256 == sha256(stored.raw).hexdigest()
    view = dict(stored.view)
    assert view["evidence_id"] == stored.evidence_id
    assert view["adopted"] is False and view["content"] is None
    assert view["citable_as_fact"] is False
    assert view["result_count"] == len(ROWS)
    for key in ("observed_at", "data_as_of", "source_start_at", "source_end_at"):
        assert key in view
    extra = {key: view.pop(key) for key in list(view) if key not in baseline.model_view}
    assert view == dict(baseline.model_view)
    assert "evidence_id" in extra and "content" not in extra


def test_byte_over_limit_body_is_not_stored():
    reg = dataclasses.replace(registration(), max_result_bytes=64, max_view_tokens=1)
    store = MemoryEvidenceStore()
    outcome = _run(store, body(ROWS), reg=reg)
    assert outcome.reason == "RESULT_TOO_LARGE"
    assert outcome.model_view["max_result_bytes"] == 64
    assert store.count_spilled("run-9") == (0, 0)


def test_run_cap_of_sixteen_records_then_refused_without_storing():
    store = MemoryEvidenceStore()
    executor, transport = _build(store)
    transport.response = TransportResponse(body=body(ROWS))
    views = []
    for index in range(17):
        outcome = executor.execute(request(step_id=f"step-{index}"))
        assert outcome.reason == "RESULT_TOO_LARGE"
        views.append(outcome.model_view)
    assert store.count_spilled("run-9")[0] == 16
    assert set(views[16]) == set(views[0])
    # Another Run is counted on its own.
    other = _run(store, body(ROWS), run_id="run-10")
    assert other.reason == "RESULT_TOO_LARGE"
    assert store.count_spilled("run-10")[0] == 1


def test_run_byte_cap_refuses_a_record_that_would_exceed_it(monkeypatch):
    store = MemoryEvidenceStore()
    _run(store, body(ROWS))
    _, first = store.count_spilled("run-9")
    monkeypatch.setattr(executor_module, "MAX_SPILLED_BYTES_PER_RUN", 2 * first - 1)
    executor, transport = _build(store)
    transport.response = TransportResponse(body=body(ROWS))
    outcome = executor.execute(request(step_id="step-2"))
    assert outcome.reason == "RESULT_TOO_LARGE"
    assert store.count_spilled("run-9") == (1, first)


def test_control_invalidated_read_keeps_its_history_path():
    control = FixedControl(
        later=ControlSnapshot(7, 0, 0, suspended=True), later_after=2
    )
    store = MemoryEvidenceStore()
    outcome = _run(store, body(ROWS), control=control)
    assert (outcome.status, outcome.reason) == ("denied", "SUSPENDED")
    assert outcome.evidence is not None and outcome.evidence.adopted is False
    assert store.count_spilled("run-9") == (0, 0)
    assert len(store._records) == 1


class _FailingStore(MemoryEvidenceStore):
    def register(self, record):
        raise RuntimeError("evidence store unavailable")


@pytest.mark.parametrize("sink", [_FailingStore(), RecordingSink()])
def test_a_sink_that_cannot_store_or_count_leaves_the_refusal_unchanged(sink):
    payload = body(ROWS)
    baseline = _run(RecordingSink(), payload)
    outcome = _run(sink, payload)
    assert canonical(outcome.model_view) == canonical(baseline.model_view)
    assert outcome.evidence is None
