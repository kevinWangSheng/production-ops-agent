"""r9 (#199) on a real PostgreSQL: an oversized traces view generates a
reviewable draft (D47), and a job capped by INPUT_TOO_LARGE under an older
input policy is due once more under the current one (D52)."""

import json
import os
from uuid import UUID

import psycopg
import pytest

from opspilot.investigation.loop import ModelError
from opspilot.knowledge import generation
from opspilot.knowledge.contract import POSTMORTEM_INPUT_POLICY_VERSION
from opspilot.knowledge.generation import MAX_VIEW_BYTES, InputTooLarge
from opspilot.knowledge.jobs import MAX_ATTEMPTS_PER_WATERMARK
from tests.integration import test_m1_03_draft_generation_postgres as _generation_tests
from tests.integration.test_m1_03_draft_generation_postgres import (
    T0,
    ScriptedModel,
    _evidence,
    _ready,
    _reply,
    _seed,
    _worker,
)

dsn = _generation_tests.dsn
stores = _generation_tests.stores
_isolated = _generation_tests._isolated

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

OLD_POLICY = "f13-postmortem-input-v2"


def _oversize_traces(dsn: str, incident_id: UUID) -> None:
    spans = [
        {
            "trace_id": f"trace-{i:06d}",
            "span_id": f"span-{i:06d}",
            "service": "checkout",
            "operation": "PlaceOrder",
            "duration_ms": i,
        }
        for i in range(600)
    ]
    view = {
        "evidence_id": _evidence(incident_id),
        "status": "ok",
        "adopted": True,
        "citable_as_fact": True,
        "tool": "traces_search",
        "target_id": "checkout",
        "window": {"start": T0.isoformat(), "end": T0.isoformat()},
        "result_count": 600,
        "returned_count": 600,
        "content": spans,
        "span_groups": [
            {"service": "checkout", "operation": "PlaceOrder", "rows": 600}
        ],
    }
    assert len(json.dumps(spans)) > MAX_VIEW_BYTES
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "UPDATE opspilot_evidence SET view=%s WHERE evidence_id=%s",
            (json.dumps(view), _evidence(incident_id)),
        )


def _too_large(raw):
    raise InputTooLarge("MAX_VIEW_BYTES")


def _job(dsn: str, incident_id: UUID) -> dict:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT consecutive_failures, next_attempt_at, last_error_code "
            "FROM opspilot_postmortem_generation_jobs WHERE incident_id=%s",
            (incident_id,),
        ).fetchone()
    return {"failures": row[0], "next_attempt_at": row[1], "error": row[2]}


def _attempts(dsn: str, incident_id: UUID) -> list[tuple[str, str | None, str]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT status, error_code, input_policy_version "
            "FROM opspilot_postmortem_generation_attempts WHERE incident_id=%s "
            "ORDER BY started_at",
            (incident_id,),
        ).fetchall()


def _capped_under_old_policy(stores, dsn, monkeypatch, incident_id: UUID) -> None:
    """The pre-r9 outcome: INPUT_TOO_LARGE under input v2 exhausts R7."""
    monkeypatch.setattr(generation, "POSTMORTEM_INPUT_POLICY_VERSION", OLD_POLICY)
    worker = _worker(stores, ScriptedModel())
    worker.input_builder = _too_large
    outcome = worker.generate(incident_id)
    monkeypatch.undo()
    assert (outcome.status, outcome.error_code) == ("failed", "INPUT_TOO_LARGE")
    job = _job(dsn, incident_id)
    assert job["failures"] == MAX_ATTEMPTS_PER_WATERMARK
    assert job["next_attempt_at"] is None
    # still capped for a scan that runs the old policy
    assert incident_id not in stores[1].candidates(
        limit=50, input_policy_version=OLD_POLICY
    )


def test_old_policy_cap_is_due_once_and_the_compacted_view_enters_review(
    stores, dsn, monkeypatch
) -> None:
    knowledge, jobs, _ = stores
    incident_id = _seed(dsn)
    _oversize_traces(dsn, incident_id)
    _capped_under_old_policy(stores, dsn, monkeypatch, incident_id)
    assert incident_id in jobs.candidates(limit=50)

    model = ScriptedModel(_reply(incident_id))
    [outcome] = _worker(stores, model).poll_once()
    assert outcome.status == "succeeded"
    [version] = knowledge.incident_postmortem(incident_id)["postmortem"]["versions"]
    assert version["state"] == "under_review"
    payload = json.loads(model.calls[0].messages[1]["content"])
    [view] = payload["evidence_views"]
    assert view["projection"] == "span_groups_only"
    assert view["omitted_content_items"] == 600 and "content" not in view
    [entry] = [
        e
        for e in payload["evidence_catalog"]
        if e["evidence_id"] == _evidence(incident_id)
    ]
    assert entry["projection"] == "span_groups_only" and entry["citable_as_fact"]
    assert _attempts(dsn, incident_id) == [
        ("failed", "INPUT_TOO_LARGE", OLD_POLICY),
        ("succeeded", None, POSTMORTEM_INPUT_POLICY_VERSION),
    ]


def test_input_too_large_again_under_the_current_policy_caps_for_good(
    stores, dsn, monkeypatch
) -> None:
    _, jobs, _ = stores
    incident_id = _seed(dsn)
    _capped_under_old_policy(stores, dsn, monkeypatch, incident_id)
    worker = _worker(stores, ScriptedModel())
    worker.input_builder = _too_large
    [outcome] = worker.poll_once()
    assert outcome.error_code == "INPUT_TOO_LARGE"
    job = _job(dsn, incident_id)
    assert job["failures"] == MAX_ATTEMPTS_PER_WATERMARK
    assert job["next_attempt_at"] is None
    assert incident_id not in jobs.candidates(limit=50)
    assert worker.poll_once() == []
    assert worker.generate(incident_id) is None
    assert [a[2] for a in _attempts(dsn, incident_id)] == [
        OLD_POLICY,
        POSTMORTEM_INPUT_POLICY_VERSION,
    ]


def test_the_reopened_round_counts_r7_afresh(stores, dsn, monkeypatch) -> None:
    _, jobs, _ = stores
    incident_id = _seed(dsn)
    _capped_under_old_policy(stores, dsn, monkeypatch, incident_id)
    worker = _worker(stores, ScriptedModel(ModelError("MODEL_UNAVAILABLE")))
    assert worker.generate(incident_id).error_code == "MODEL_UNAVAILABLE"
    job = _job(dsn, incident_id)
    assert job["failures"] == 1 and job["next_attempt_at"] is not None
    for count in range(2, MAX_ATTEMPTS_PER_WATERMARK + 1):
        _ready(dsn, incident_id)
        assert incident_id in jobs.candidates(limit=50)
        worker.model = ScriptedModel(ModelError("MODEL_UNAVAILABLE"))
        assert worker.generate(incident_id).error_code == "MODEL_UNAVAILABLE"
        assert _job(dsn, incident_id)["failures"] == count
    # capped by a provider error under the current policy: not reopened
    assert _job(dsn, incident_id)["next_attempt_at"] is None
    assert incident_id not in jobs.candidates(limit=50)


def test_a_cap_from_other_errors_under_the_old_policy_is_not_reopened(
    stores, dsn, monkeypatch
) -> None:
    _, jobs, _ = stores
    incident_id = _seed(dsn)
    monkeypatch.setattr(generation, "POSTMORTEM_INPUT_POLICY_VERSION", OLD_POLICY)
    worker = _worker(stores, ScriptedModel())
    for _ in range(MAX_ATTEMPTS_PER_WATERMARK):
        _ready(dsn, incident_id)
        worker.model = ScriptedModel(ModelError("MODEL_UNAVAILABLE"))
        assert worker.generate(incident_id).error_code == "MODEL_UNAVAILABLE"
    monkeypatch.undo()
    job = _job(dsn, incident_id)
    assert (job["failures"], job["next_attempt_at"], job["error"]) == (
        MAX_ATTEMPTS_PER_WATERMARK,
        None,
        "MODEL_UNAVAILABLE",
    )
    assert incident_id not in jobs.candidates(limit=50)
    assert worker.generate(incident_id) is None
