"""Real isolated PostgreSQL claims; network services are never called."""

import concurrent.futures
import os
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest

from scripts.m0.config import ConfigError
from scripts.m0.live import LiveLedger, utcnow
from scripts.m0.postgres_lab import DSN

pytestmark = pytest.mark.skipif(
    os.environ.get("M0_B_POSTGRES") != "1",
    reason="explicit local PostgreSQL opt-in required",
)


def packet():
    return {
        "experiment_id": str(uuid4()),
        "run_id": str(uuid4()),
        "approval_ref": str(uuid4()),
        "deadline": (utcnow() + timedelta(minutes=5)).isoformat(),
    }


def claim(contract):
    try:
        LiveLedger(DSN).claim(contract)
        return True
    except ConfigError:
        return False


def test_persistent_one_shot_and_approval_identity():
    ledger = LiveLedger(DSN)
    ledger.install_live()
    contract = packet()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(claim, [contract] * 4))
    assert results.count(True) == 1
    assert not claim(contract)  # Fresh connection/ledger does not reset authorization.
    assert not claim(contract | {"experiment_id": str(uuid4()), "run_id": str(uuid4())})
    ledger.attempt(contract["experiment_id"], "project")
    with pytest.raises(ConfigError):
        ledger.attempt(contract["experiment_id"], "project")
    ledger.save(contract["experiment_id"], "completed", {"status": "completed"}, [])
    ledger.trace_status(contract["experiment_id"], "unknown")
    with psycopg.connect(DSN) as conn:
        row = conn.execute(
            "SELECT reserved_cny,cost_state,business,outbox,trace_status FROM m0_live_once WHERE experiment_id=%s",
            (contract["experiment_id"],),
        ).fetchone()
    assert tuple(map(str, row[:3])) == ("2.00", "unreconciled", "completed")
    assert row[3:] == ({"status": "completed"}, "unknown")


def test_expired_claim_and_attempt():
    ledger = LiveLedger(DSN)
    ledger.install_live()
    contract = packet()
    assert not claim(
        contract | {"deadline": (utcnow() - timedelta(seconds=1)).isoformat()}
    )
    ledger.claim(contract)
    with psycopg.connect(DSN) as conn:
        conn.execute(
            "UPDATE m0_live_once SET deadline=clock_timestamp()-interval '1 second' WHERE experiment_id=%s",
            (contract["experiment_id"],),
        )
    with pytest.raises(ConfigError):
        ledger.attempt(contract["experiment_id"], "model-1")
