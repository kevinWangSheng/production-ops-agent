"""#308 PR2 implementer tests against PostgreSQL under the real worker role.

The product otel-demo executor keeps an over-limit result through
``DurableEvidenceStore`` connected as a throwaway ``WORKER_ROLE`` login, the
loop's commit pins the kept refusal as the model saw it, and ``read_evidence``
reads it back with the worker's existing SELECT grant: no migration, no new
grant (contract r1, R3).
"""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Iterator
from datetime import timedelta

import psycopg
import pytest
from psycopg.rows import dict_row

from opspilot.persistence import DurableStore, PoolConfig
from opspilot.persistence.grants import WORKER_ROLE
from opspilot.tools.evidence_read import EVIDENCE_TOOL
from opspilot.tools.otel_demo import (
    METRICS_TOOL,
    OtelDemoConfig,
    otel_demo_executor_factory,
)
from opspilot.tools.registry import canonical
from opspilot.web.evidence import DurableEvidenceStore
from scripts.m0.postgres_lab import DSN
from tests.m1_tool_support import FakeClock
from tests.runtime_roles_support import login
from tests.test_308_pr2_read_evidence_impl import ROWS, _matrix, quarter_counter
from tests.test_m1_otel_demo_contract import (
    GOOD_EXPR,
    JAEGER_URL,
    NOW,
    PROMETHEUS_URL,
    FakeOpener,
    FakeStore,
    _call,
    _input,
    _lease,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required",
)

POOL = PoolConfig(min_size=1, max_size=4, timeout=5.0)


@pytest.fixture(scope="module")
def worker_evidence() -> Iterator[DurableEvidenceStore]:
    with login(DSN, WORKER_ROLE) as dsn:
        store = DurableStore(dsn, pool=POOL)
        yield DurableEvidenceStore(store)
        store.close()


def _executor(monkeypatch, evidence):
    store = FakeStore(_lease(), deadline=NOW + timedelta(minutes=10))
    opener = FakeOpener(by_query={GOOD_EXPR: _matrix(ROWS)})
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: opener)
    factory = otel_demo_executor_factory(
        store,
        evidence=evidence,
        clock=FakeClock(start=NOW),
        config=OtelDemoConfig(
            prometheus_url=PROMETHEUS_URL, jaeger_url=JAEGER_URL, token=None
        ),
        token_counter=quarter_counter,
    )
    return factory(store.lease, _input(str(store.lease.run_id)))


def _row(evidence_id: str) -> dict[str, object]:
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT * FROM opspilot_evidence WHERE evidence_id=%s", (evidence_id,)
        ).fetchone()
    assert row is not None
    return row


def test_worker_role_keeps_pins_and_reads_back_an_over_limit_result(
    monkeypatch, worker_evidence
):
    executor = _executor(monkeypatch, worker_evidence)
    refused = executor.execute(_call(METRICS_TOOL, {"expr": GOOD_EXPR}))
    view = dict(refused.model_view)
    assert refused.reason == "RESULT_TOO_LARGE" and refused.evidence is not None
    evidence_id = view["evidence_id"]
    # The loop commits the tool result; the projection pins it (R2).
    worker_evidence.commit(evidence_id, view)
    row = _row(evidence_id)
    assert row["committed"] is True and row["adopted"] is False
    assert canonical(row["view"]) == canonical(view)

    sliced = executor.execute(
        _call(
            EVIDENCE_TOOL,
            {"evidence_id": evidence_id, "offset": 3, "limit": 4},
            index=1,
        )
    )
    assert sliced.status == "ok" and sliced.adopted
    parent_rows = json.loads(bytes(row["raw"]))["data"]["result"]
    assert sliced.model_view["content"] == parent_rows[3:7]
    worker_evidence.commit(sliced.model_view["evidence_id"], dict(sliced.model_view))
    slice_row = _row(sliced.model_view["evidence_id"])
    assert slice_row["adopted"] is True and slice_row["status"] == "ok"
    assert slice_row["observed_at"] == row["observed_at"]  # U4: parent's time
    stored = worker_evidence.get(sliced.model_view["evidence_id"])
    assert stored is not None and stored.hashes_verified
