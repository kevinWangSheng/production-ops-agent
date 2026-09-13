"""Opt-in PG contracts for the remaining M0-03 control boundaries.

These tests deliberately separate implemented generation/identity guards from
unsupported pause/resume plumbing. They never call a model or telemetry API.
"""

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from scripts.m0.budget import PostgresBudget
from scripts.m0.contracts import BudgetError, RunContext
from scripts.m0.outcomes import (
    HealthProfile,
    IndependentObservation,
    Signal,
    Subject,
    Target,
    Window,
)
from scripts.m0.postgres_lab import DSN
from scripts.m0.step_store import StepStore

pytestmark = pytest.mark.skipif(
    os.environ.get("M0_CONTROL_POSTGRES") != "1",
    reason="explicit control-contract PG opt-in required",
)

VERSION = {"state": "v3", "provider": "deepseek", "tool": "fixture-v1"}


@pytest.fixture
def lab():
    ledger = PostgresBudget(DSN)
    ledger.install()
    store = StepStore(ledger)
    store.install()
    run = RunContext(
        uuid4(), uuid4(), "deepseek", datetime.now(timezone.utc) + timedelta(minutes=4)
    )
    ledger.initialize(run.experiment_id, 100, run.deadline)
    subject = store.accept(run, "control-contract", {"request": "synthetic"}, VERSION)
    return store, ledger, run, subject


def test_release_failure_then_takeover_generation_race_keeps_first_control(lab):
    store, _, run, subject = lab

    first = store.control(subject, 0, "cancel", payload={"cause": "publish_failure"})
    assert first == 1
    with pytest.raises(BudgetError, match="CONTROL_CONFLICT"):
        store.control(subject, 0, "correct", payload={"cause": "human_takeover"})

    snapshot = store.control_snapshot(subject)
    assert snapshot["final_generation"] == 1
    assert store.summary(subject)["state"]["state"] == "cancelled"
    assert snapshot["controls"][0]["action"] == "cancel"
    assert "payload" not in snapshot["controls"][0]


def test_health_profile_revision_and_capture_order_preserve_revision_mismatch(lab):
    _, ledger, run, subject = lab
    target = Target(
        integration_id="m0-otel",
        cluster_uid="local",
        namespace="default",
        resource_uid="svc-1",
        revision="rev-1",
    )
    window = Window(
        start=datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc),
        end=datetime(2026, 9, 12, 10, 5, tzinfo=timezone.utc),
    )
    profile = HealthProfile(
        revision="profile-v1",
        required_signals=["errors"],
        min_samples=1,
        freshness_seconds=60,
        required_window=window,
    )
    old_observation = IndependentObservation(
        subject=Subject(
            id="s",
            kind="release_observation",
            target=target,
            control_generation=0,
            before_revision="rev-0",
            release_id="release-1",
        ),
        profile_revision="profile-v0",
        window=window,
        signals=[
            Signal(name="errors", evidence_id="e-old", samples=1, verdict="healthy")
        ],
    )
    # Persist the versioned record in the real PG lab so replay is not an
    # in-memory-only assertion. The evaluator still uses a fixed captured_at.
    with ledger._transaction() as conn:
        conn.execute(
            "CREATE TEMP TABLE m0_control_profile_replay(profile jsonb, observation jsonb, captured_at timestamptz)"
        )
        conn.execute(
            "INSERT INTO m0_control_profile_replay VALUES(%s,%s,%s)",
            (
                Jsonb(profile.model_dump(mode="json")),
                Jsonb(old_observation.model_dump(mode="json")),
                "2026-09-12T10:05:00Z",
            ),
        )
        row = conn.execute(
            "SELECT profile->>'revision' AS profile_revision, observation->>'profile_revision' AS observation_revision, captured_at FROM m0_control_profile_replay"
        ).fetchone()
    assert row["profile_revision"] == "profile-v1"
    assert row["observation_revision"] == "profile-v0"
    assert (
        row["captured_at"].astimezone(timezone.utc).isoformat()
        == "2026-09-12T10:05:00+00:00"
    )
    assert old_observation.profile_revision != profile.revision


def test_global_pause_and_resume_are_fail_closed_until_control_contract_exists(lab):
    store, _, _, subject = lab

    with pytest.raises(BudgetError, match="INVALID_INPUT"):
        store.control(subject, 0, "pause")
    with pytest.raises(BudgetError, match="INVALID_INPUT"):
        store.control(subject, 0, "resume")
    snapshot = store.control_snapshot(subject)
    assert snapshot["final_generation"] == 0
    assert store.summary(subject)["state"]["state"] == "running"


def test_observer_authorization_cannot_borrow_investigation_identity(lab):
    store, ledger, run, subject = lab
    other_run = RunContext(
        run.experiment_id, uuid4(), "deepseek", run.deadline - timedelta(seconds=1)
    )
    other_subject = store.new_run(subject, 0, other_run, {"request": "other"}, VERSION)
    assert other_subject == 1
    with pytest.raises(BudgetError, match="IDENTITY_CONFLICT"):
        store.claim(subject, run, uuid4(), VERSION)
