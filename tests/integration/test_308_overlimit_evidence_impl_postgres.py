"""#308 PR1 implementer tests against PostgreSQL under the real worker role.

The executor registers an over-limit view's redacted raw body as un-adopted
evidence through ``DurableEvidenceStore`` connected as a throwaway login in
``WORKER_ROLE``: the existing evidence grants (SELECT, INSERT, UPDATE) suffice,
no migration. ``count_spilled`` counts only those records, per Run, and the
per-Run cap of 16 holds against the stored rows, also when parallel workers
spill into one Run at once (R13).
"""

from __future__ import annotations

import dataclasses
import os
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

import opspilot.tools.executor as executor_module
from opspilot.persistence import DurableStore, PoolConfig
from opspilot.persistence.grants import WORKER_ROLE
from opspilot.tools import (
    ReadOnlyToolExecutor,
    TargetRegistry,
    ToolRegistry,
    TransportResponse,
)
from opspilot.tools.registry import canonical
from opspilot.web.evidence import DurableEvidenceStore
from scripts.m0.postgres_lab import DSN
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
from tests.runtime_roles_support import login
from tests.test_308_overlimit_evidence_impl import racing

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required",
)

POOL = PoolConfig(min_size=1, max_size=4, timeout=5.0)
ROWS = [
    {"metric": {"msg": f"login password=xyz row {index}"}, "value": index}
    for index in range(12)
]


def bytes_counter(text: str) -> int:
    return len(text.encode("utf-8"))


@pytest.fixture(scope="module")
def worker_evidence() -> Iterator[DurableEvidenceStore]:
    with login(DSN, WORKER_ROLE) as dsn:
        store = DurableStore(dsn, pool=POOL)
        yield DurableEvidenceStore(store)
        store.close()


def _executor(sink, run_id, *, reg=None):
    clock = FakeClock()
    reg = reg or dataclasses.replace(
        registration(), max_view_tokens=64, max_result_bytes=1 << 20
    )
    tools = ToolRegistry([reg])
    targets = TargetRegistry([target()])
    transport = FakeTransport(clock=clock)
    transport.response = TransportResponse(body=body(ROWS))
    return ReadOnlyToolExecutor(
        scope=scope(targets, tool_registry=tools, run_id=run_id),
        tools=tools,
        targets=targets,
        transport=transport,
        evidence=sink,
        control=FixedControl(),
        clock=clock,
        ledger=RecordingLedger(),
        token_counter=bytes_counter,
    )


def _rows(run_id: str) -> list[dict[str, object]]:
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT * FROM opspilot_evidence WHERE run_id=%s ORDER BY evidence_id",
            (run_id,),
        ).fetchall()


def test_worker_role_stores_the_over_limit_result_unadopted(worker_evidence):
    run_id = f"run-{uuid4()}"
    baseline = _executor(RecordingSink(), run_id).execute(request())
    outcome = _executor(worker_evidence, run_id).execute(request())

    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert canonical(outcome.model_view) == canonical(baseline.model_view)
    assert outcome.evidence is None

    (row,) = _rows(run_id)
    assert row["adopted"] is False and row["status"] == "error"
    assert row["committed"] is False
    assert row["view"]["reason"] == "RESULT_TOO_LARGE"
    assert row["view"]["evidence_id"] == row["evidence_id"]
    raw = bytes(row["raw"])
    assert b"xyz" not in raw and b"[REDACTED_CREDENTIAL]" in raw
    stored = worker_evidence.get(row["evidence_id"])
    assert stored is not None and stored.hashes_verified
    assert stored.run_id == run_id
    assert worker_evidence.count_spilled(run_id) == (1, len(raw))
    assert worker_evidence.count_spilled(f"run-{uuid4()}") == (0, 0)


def test_worker_role_cap_of_sixteen_per_run(worker_evidence):
    run_id = f"run-{uuid4()}"
    executor = _executor(worker_evidence, run_id)
    for index in range(17):
        outcome = executor.execute(request(step_id=f"step-{index}"))
        assert outcome.reason == "RESULT_TOO_LARGE"
        assert outcome.evidence is None
    assert worker_evidence.count_spilled(run_id)[0] == 16
    assert len(_rows(run_id)) == 16


def test_byte_over_limit_is_not_stored(worker_evidence):
    run_id = f"run-{uuid4()}"
    reg = dataclasses.replace(registration(), max_result_bytes=64, max_view_tokens=1)
    outcome = _executor(worker_evidence, run_id, reg=reg).execute(request())
    assert outcome.reason == "RESULT_TOO_LARGE"
    assert _rows(run_id) == []


THREADS = 6


@pytest.fixture
def racing_evidence():
    """A worker-role store per test whose spill admission lines threads up."""
    barrier = threading.Barrier(THREADS, timeout=5)
    with login(DSN, WORKER_ROLE) as dsn:
        store = DurableStore(
            dsn, pool=PoolConfig(min_size=1, max_size=THREADS + 1, timeout=10.0)
        )
        yield racing(DurableEvidenceStore, barrier)(store), barrier
        store.close()


def _spill_concurrently(evidence, run_id: str) -> None:
    executors = [_executor(evidence, run_id) for _ in range(THREADS)]
    with ThreadPoolExecutor(THREADS) as pool:
        outcomes = list(
            pool.map(
                lambda pair: pair[1].execute(request(step_id=f"race-{pair[0]}")),
                enumerate(executors),
            )
        )
    assert {outcome.reason for outcome in outcomes} == {"RESULT_TOO_LARGE"}


def test_parallel_workers_at_fifteen_store_exactly_sixteen(racing_evidence):
    evidence, barrier = racing_evidence
    run_id = f"run-{uuid4()}"
    barrier.abort()  # the prefill runs alone
    executor = _executor(evidence, run_id)
    for index in range(15):
        executor.execute(request(step_id=f"step-{index}"))
    assert len(_rows(run_id)) == 15
    barrier.reset()

    _spill_concurrently(evidence, run_id)
    barrier.abort()

    assert len(_rows(run_id)) == 16


def test_parallel_workers_never_pass_the_run_byte_cap(racing_evidence, monkeypatch):
    evidence, barrier = racing_evidence
    run_id = f"run-{uuid4()}"
    barrier.abort()
    _executor(evidence, run_id).execute(request())
    (row,) = _rows(run_id)
    first = len(bytes(row["raw"]))
    # Room for exactly one more record of the same size.
    monkeypatch.setattr(executor_module, "MAX_SPILLED_BYTES_PER_RUN", 3 * first - 1)
    barrier.reset()

    _spill_concurrently(evidence, run_id)
    barrier.abort()

    rows = _rows(run_id)
    assert len(rows) == 2
    assert sum(len(bytes(row["raw"])) for row in rows) == 2 * first
