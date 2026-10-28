"""PostgreSQL acceptance for #308 PR2: over-limit refusal ids and read_evidence.

Same opt-in as the PR1 module.  The evidence rows live in the real
``opspilot_evidence`` table through ``DurableEvidenceStore``; the Run, control
and budget tables are not needed (the in-memory ``FakeStore`` of the OTel
contract tests answers those three reads) and the source is the deterministic
``FakeOpener``.  Each test uses its own Run ids, so the persistent lab is only
ever read for rows the test created.
"""

from __future__ import annotations

import json
import os

import pytest

from opspilot.persistence import DurableStore
from opspilot.tools.registry import canonical
from opspilot.web import DurableEvidenceStore
from scripts.m0.postgres_lab import DSN
from tests.acceptance.test_308_pr2_read_evidence import (
    SERIES,
    _Lab,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)


@pytest.fixture()
def store():
    durable = DurableStore(DSN)
    durable.install()
    try:
        yield durable
    finally:
        durable.close()


@pytest.fixture()
def lab(monkeypatch, store):
    return _Lab(monkeypatch, sink=DurableEvidenceStore(store))


def _rows(store: DurableStore, run_id: str):
    with store.transaction(snapshot=True) as conn:
        return conn.execute(
            "SELECT evidence_id,status,adopted,raw,raw_sha256,view,view_sha256 "
            "FROM opspilot_evidence WHERE run_id=%s ORDER BY observed_at,evidence_id",
            (run_id,),
        ).fetchall()


def test_pg_refusal_view_id_resolves_to_the_unadopted_row(lab, store):
    view = lab.spill().model_view
    rows = _rows(store, lab.executor.scope.run_id)

    assert [row["evidence_id"] for row in rows] == [view["evidence_id"]]
    row = rows[0]
    assert row["adopted"] is False and row["status"] == "error"
    assert canonical(row["view"]) == canonical(view)
    stored = lab.sink.get(view["evidence_id"])
    assert stored is not None and stored.hashes_verified is True


def test_pg_read_evidence_returns_parent_rows_and_persists_an_adopted_slice(lab, store):
    parent = lab.spill().model_view
    parent_raw = bytes(_rows(store, lab.executor.scope.run_id)[0]["raw"])
    expected = json.loads(parent_raw)["data"]["result"][20:35]
    before = lab.requests

    outcome = lab.read(parent["evidence_id"], 20, 15)
    view = outcome.model_view

    assert outcome.status == "ok", view
    assert view["content"] == expected
    assert view["slice"]["total_rows"] == SERIES
    assert view["slice"]["returned"] == 15
    assert lab.requests == before
    by_id = {row["evidence_id"]: row for row in _rows(store, lab.executor.scope.run_id)}
    assert set(by_id) == {parent["evidence_id"], view["evidence_id"]}
    persisted = by_id[view["evidence_id"]]
    assert persisted["adopted"] is True and persisted["status"] == "ok"
    assert canonical(persisted["view"]) == canonical(view)
    assert by_id[parent["evidence_id"]]["adopted"] is False  # parent untouched
    stored = lab.sink.get(view["evidence_id"])
    assert stored is not None and stored.hashes_verified is True


def test_pg_other_run_and_adopted_records_are_not_readable(lab, store):
    parent = lab.spill().model_view
    adopted = lab.query(lab.serve("small_0", lab.rows[:3]))
    assert adopted.status == "ok"
    _, other = lab.new_run()
    before = lab.requests

    cross = lab.read(parent["evidence_id"], executor=other)
    ok_record = lab.read(adopted.model_view["evidence_id"])
    missing = lab.read("no-such-run-t0:00000000-0000-0000-0000-000000000000")

    for outcome in (cross, ok_record, missing):
        assert (outcome.status, outcome.reason) == ("denied", "EVIDENCE_NOT_READABLE")
        assert outcome.model_view.get("content") is None
    assert lab.requests == before
    assert len(_rows(store, other.scope.run_id)) == 0


def test_pg_seventeenth_overlimit_result_is_not_persisted_and_says_so(lab, store):
    views = [lab.spill(f"big_{i}").model_view for i in range(17)]

    rows = _rows(store, lab.executor.scope.run_id)
    assert len(rows) == 16
    assert views[16]["stored"] is False
    assert all(view.get("stored", True) is True for view in views[:16])
    persisted_ids = {row["evidence_id"] for row in rows}
    assert views[16].get("evidence_id") not in persisted_ids
