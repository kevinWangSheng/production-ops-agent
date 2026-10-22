"""PostgreSQL evidence-row acceptance for #308 PR1.

The lab database is opt-in.  The tests use a unique Run id and only read rows
they created, so the persistent lab is not cleaned or altered destructively.
Backend bytes are still supplied by the deterministic in-process transport
double; no real model, kind or kubectl call is made.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
from uuid import uuid4

import pytest

from opspilot.persistence import DurableStore
from opspilot.tools import ReadOnlyToolExecutor, ToolRequest, TransportResponse
from opspilot.tools.outcomes import PROJECTION_REVISION
from opspilot.tools.registry import (
    TargetRegistry,
    ToolRegistry,
    canonical_hash,
)
from opspilot.web import DurableEvidenceStore
from scripts.m0.postgres_lab import DSN
from tests.acceptance.test_308_pr1_overlimit_evidence import (
    NEW_PROJECTION_REVISION,
    _byte_counter,
    _FixedClock,
    _redacted_body,
    _rows,
)
from tests.m1_tool_support import (
    FakeTransport,
    FixedControl,
    RecordingLedger,
    body,
    registration,
    request,
    scope,
    target,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)


def _executor(store: DurableStore, run_id: str, *, max_result_bytes=1_000_000):
    evidence = DurableEvidenceStore(store)
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
        scope=scope(targets, tool_registry=tools, run_id=run_id),
        tools=tools,
        targets=targets,
        transport=transport,
        evidence=evidence,
        control=FixedControl(),
        clock=_FixedClock(),
        ledger=RecordingLedger(),
        token_counter=_byte_counter,
    )
    return executor, transport


def _rows_for_run(store: DurableStore, run_id: str):
    with store.transaction(snapshot=True) as conn:
        return conn.execute(
            "SELECT evidence_id,run_id,status,adopted,raw,raw_sha256,view,"
            "view_sha256,projection_revision FROM opspilot_evidence "
            "WHERE run_id=%s ORDER BY evidence_id",
            (run_id,),
        ).fetchall()


def _overlimit_request(index: int) -> ToolRequest:
    base = request()
    return dataclasses.replace(base, tool_index=index)


@pytest.fixture()
def store():
    durable = DurableStore(DSN)
    durable.install()
    try:
        yield durable
    finally:
        durable.close()


def test_pg_view_overlimit_persists_redacted_non_adopted_row(store):
    run_id = f"run-308-pr1-{uuid4()}"
    executor, transport = _executor(store, run_id)
    transport.response = TransportResponse(body=body(_rows()))

    outcome = executor.execute(_overlimit_request(0))
    rows = _rows_for_run(store, run_id)

    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    # PR2 (R1): the stored record is attached and is the model view.
    assert outcome.evidence is not None
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row["evidence_id"], str) and row["evidence_id"]
    assert row["run_id"] == run_id
    assert row["status"] == "error"
    assert row["adopted"] is False
    assert row["projection_revision"] == NEW_PROJECTION_REVISION
    assert bytes(row["raw"]) == _redacted_body()
    assert row["raw_sha256"] == hashlib.sha256(bytes(row["raw"])).hexdigest()
    assert row["view_sha256"] == canonical_hash(row["view"])
    stored = DurableEvidenceStore(store).get(row["evidence_id"])
    assert stored is not None and stored.hashes_verified is True


def test_pg_byte_limit_does_not_persist_a_row(store):
    run_id = f"run-308-pr1-byte-{uuid4()}"
    executor, transport = _executor(store, run_id, max_result_bytes=32)
    transport.response = TransportResponse(body=b"x" * 64)

    outcome = executor.execute(_overlimit_request(0))

    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert _rows_for_run(store, run_id) == []


def test_pg_seventeenth_overlimit_result_is_not_persisted(store):
    run_id = f"run-308-pr1-cap-{uuid4()}"
    executor, transport = _executor(store, run_id)
    transport.response = TransportResponse(body=body(_rows()))

    outcomes = [executor.execute(_overlimit_request(index)) for index in range(17)]

    assert all(
        (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
        for outcome in outcomes
    )
    assert len(_rows_for_run(store, run_id)) == 16


def test_pg_control_denial_does_not_persist_a_row(store):
    run_id = f"run-308-pr1-control-{uuid4()}"
    evidence = DurableEvidenceStore(store)
    tools = ToolRegistry([dataclasses.replace(registration(), max_view_tokens=25_000)])
    targets = TargetRegistry([target()])
    transport = FakeTransport(response=TransportResponse(body=body(_rows())))
    executor = ReadOnlyToolExecutor(
        scope=scope(targets, tool_registry=tools, run_id=run_id),
        tools=tools,
        targets=targets,
        transport=transport,
        evidence=evidence,
        control=FixedControl(suspended=True),
        clock=_FixedClock(),
        ledger=RecordingLedger(),
        token_counter=_byte_counter,
    )

    outcome = executor.execute(_overlimit_request(0))

    assert (outcome.status, outcome.reason) == ("denied", "SUSPENDED")
    assert _rows_for_run(store, run_id) == []
    assert transport.called is False


def test_pg_pr2_projection_revision_is_v9():
    assert PROJECTION_REVISION == NEW_PROJECTION_REVISION
