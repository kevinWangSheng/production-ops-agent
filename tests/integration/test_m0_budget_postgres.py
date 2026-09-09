"""Opt-in real PostgreSQL; isolated synthetic identities, never clears tables."""

import multiprocessing
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
import pytest

from scripts.m0.budget import PostgresBudget
from scripts.m0.contracts import BudgetError, RequestIdentity, RunContext
from scripts.m0.postgres_lab import DSN, start, stop

pytestmark = pytest.mark.skipif(
    os.environ.get("M0_B_POSTGRES") != "1",
    reason="explicit local PostgreSQL opt-in required",
)


@pytest.fixture
def lab():
    ledger = PostgresBudget(DSN)
    ledger.install()
    run = RunContext(
        uuid4(), uuid4(), "deepseek", datetime.now(timezone.utc) + timedelta(minutes=5)
    )
    ledger.initialize(run.experiment_id, 100, run.deadline)
    ledger.register_run(run)
    return ledger, run


def invoke(identity, upper, output):
    try:
        output.put(PostgresBudget(DSN).reserve(identity, upper).created)
    except BudgetError as error:
        output.put(str(error))


def concurrent(requests, upper):
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    processes = [
        context.Process(target=invoke, args=(identity, upper, output))
        for identity in requests
    ]
    for process in processes:
        process.start()
    results = [output.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    return results


def test_cross_process_competition_and_shared_runs(lab):
    ledger, run = lab
    other = RunContext(run.experiment_id, uuid4(), run.provider, run.deadline)
    ledger.register_run(other)
    results = concurrent(
        [RequestIdentity(r, uuid4()) for r in (run, other, run, other)], 40
    )
    assert results.count(True) == 2
    assert results.count("BUDGET_EXHAUSTED") == 2
    assert ledger.snapshot(run.experiment_id)["reserved"] == 80


def test_same_request_only_one_sender_and_conflicts(lab):
    ledger, run = lab
    identity = RequestIdentity(run, uuid4())
    results = concurrent([identity] * 4, 25)
    assert results.count(True) == 1 and results.count(False) == 3
    assert ledger.snapshot(run.experiment_id)["reserved"] == 25
    with pytest.raises(BudgetError, match="IDENTITY_CONFLICT"):
        ledger.reserve(identity, 26)
    with pytest.raises(BudgetError, match="IDENTITY_CONFLICT"):
        ledger.initialize(run.experiment_id, 101, run.deadline)
    with pytest.raises(BudgetError, match="IDENTITY_CONFLICT"):
        ledger.register_run(
            RunContext(
                run.experiment_id,
                run.run_id,
                run.provider,
                run.deadline + timedelta(seconds=1),
            )
        )


def test_unknown_settlement_overage_and_block(lab):
    ledger, run = lab
    identity = RequestIdentity(run, uuid4())
    ledger.reserve(identity, 50)
    assert ledger.retain_unknown(identity).state == "unknown"
    assert ledger.retain_unknown(identity).state == "unknown"
    assert ledger.snapshot(run.experiment_id)["unknown"] == 50
    assert ledger.settle(identity, 120).actual == 120
    assert ledger.settle(identity, 120).actual == 120
    assert ledger.retain_unknown(identity).state == "settled"
    assert ledger.reserve(identity, 50).created is False
    with pytest.raises(BudgetError, match="SETTLEMENT_CONFLICT"):
        ledger.settle(identity, 1)
    assert ledger.snapshot(run.experiment_id) == dict(
        limit=100, settled=120, reserved=0, unknown=0, blocked=True
    )
    with pytest.raises(BudgetError, match="BUDGET_EXHAUSTED"):
        ledger.reserve(RequestIdentity(run, uuid4()), 1)


def test_release_exact_cost_and_unknown_identity(lab):
    ledger, run = lab
    identity = RequestIdentity(run, uuid4())
    with pytest.raises(BudgetError, match="UNKNOWN_IDENTITY"):
        ledger.settle(identity, 0)
    ledger.reserve(identity, 100)
    ledger.settle(identity, 30)
    assert ledger.reserve(RequestIdentity(run, uuid4()), 70).created
    assert ledger.snapshot(run.experiment_id)["settled"] == 30


def test_deadline_checked_after_lock_wait(lab):
    ledger, original = lab
    run = RunContext(
        original.experiment_id,
        uuid4(),
        original.provider,
        datetime.now(timezone.utc) + timedelta(seconds=1),
    )
    ledger.register_run(run)
    identity = RequestIdentity(run, uuid4())
    result = []

    def waiting():
        try:
            ledger.reserve(identity, 1)
        except BudgetError as error:
            result.append(str(error))

    with psycopg.connect(DSN) as blocker:
        blocker.execute(
            "SELECT * FROM m0_experiments WHERE id=%s FOR UPDATE", (run.experiment_id,)
        )
        worker = threading.Thread(target=waiting)
        worker.start()
        # Observe an actual lock waiter, not merely a thread start.
        for _ in range(50):
            with psycopg.connect(DSN) as observer:
                found = observer.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname='m0_budget' AND wait_event_type='Lock'"
                ).fetchone()[0]
            if found:
                break
            time.sleep(0.02)
        assert found
        time.sleep(1.1)
    worker.join(6)
    assert result == ["DEADLINE_EXCEEDED"]
    assert ledger.snapshot(run.experiment_id)["reserved"] == 0


@pytest.mark.skipif(
    os.environ.get("M0_B_RESTART") != "1",
    reason="dedicated cluster restart requires explicit opt-in",
)
def test_database_restart_retains_unknown_and_denies_when_down(lab):
    ledger, run = lab
    identity = RequestIdentity(run, uuid4())
    ledger.reserve(identity, 80)
    ledger.retain_unknown(identity)
    before = ledger.snapshot(run.experiment_id)
    stop()
    try:
        with pytest.raises(BudgetError, match="STORAGE_UNAVAILABLE"):
            ledger.reserve(RequestIdentity(run, uuid4()), 1)
    finally:
        start()
    recovered = PostgresBudget(DSN)
    recovered.initialize(run.experiment_id, 100, run.deadline)
    recovered.register_run(run)
    assert recovered.snapshot(run.experiment_id) == before
    assert recovered.reserve(identity, 80).created is False
    with pytest.raises(BudgetError, match="BUDGET_EXHAUSTED"):
        recovered.reserve(RequestIdentity(run, uuid4()), 21)


def test_identity_conflicts_do_not_mutate_other_record(lab):
    ledger, run = lab
    identity = RequestIdentity(run, uuid4())
    ledger.reserve(identity, 10)
    other = RunContext(uuid4(), uuid4(), "deepseek", run.deadline)
    ledger.initialize(other.experiment_id, 100, other.deadline)
    ledger.register_run(other)
    mismatch = RequestIdentity(other, identity.request_id)
    for call in (
        lambda: ledger.reserve(mismatch, 10),
        lambda: ledger.settle(mismatch, 0),
        lambda: ledger.retain_unknown(mismatch),
    ):
        with pytest.raises(BudgetError, match="IDENTITY_CONFLICT"):
            call()
    assert ledger.snapshot(run.experiment_id)["reserved"] == 10
    assert ledger.snapshot(other.experiment_id)["reserved"] == 0


def test_expired_history_can_settle_without_new_authority(lab):
    ledger, original = lab
    run = RunContext(
        original.experiment_id,
        uuid4(),
        "deepseek",
        datetime.now(timezone.utc) + timedelta(milliseconds=150),
    )
    ledger.register_run(run)
    identity = RequestIdentity(run, uuid4())
    ledger.reserve(identity, 100)
    time.sleep(0.2)
    assert ledger.reserve(identity, 100).created is False
    assert ledger.retain_unknown(identity).state == "unknown"
    assert ledger.settle(identity, 0).actual == 0
    with pytest.raises(BudgetError, match="DEADLINE_EXCEEDED"):
        ledger.reserve(RequestIdentity(run, uuid4()), 1)


def test_large_integers_preserve_exact_budget():
    ledger = PostgresBudget(DSN)
    run = RunContext(
        uuid4(), uuid4(), "deepseek", datetime.now(timezone.utc) + timedelta(minutes=1)
    )
    ceiling = 10**35 + 7
    ledger.initialize(run.experiment_id, ceiling, run.deadline)
    ledger.register_run(run)
    ledger.reserve(RequestIdentity(run, uuid4()), ceiling)
    with pytest.raises(BudgetError, match="BUDGET_EXHAUSTED"):
        ledger.reserve(RequestIdentity(run, uuid4()), 1)
    assert ledger.snapshot(run.experiment_id)["reserved"] == ceiling


def test_lost_commit_acknowledgment_never_grants_second_send(lab, monkeypatch):
    from contextlib import contextmanager

    import scripts.m0.budget as module

    ledger, run = lab
    identity = RequestIdentity(run, uuid4())
    connect = psycopg.connect

    @contextmanager
    def lose_ack(*args, **kwargs):
        with connect(*args, **kwargs) as conn:
            yield conn
        raise psycopg.OperationalError("synthetic secret connection detail")

    with monkeypatch.context() as scoped:
        scoped.setattr(module.psycopg, "connect", lose_ack)
        with pytest.raises(BudgetError, match="STORAGE_UNAVAILABLE") as error:
            ledger.reserve(identity, 60)
        assert error.value.__context__ is None
    assert ledger.reserve(identity, 60).created is False
    assert ledger.snapshot(run.experiment_id)["reserved"] == 60
