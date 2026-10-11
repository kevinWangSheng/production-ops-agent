"""Independent #199 D52: old INPUT_TOO_LARGE gets one new R7 round.

Only storage public operations run; no worker/model/network calls. Historical
attempts are created with the old recorded policy, using the same public lease
and finish operations as real generation. Owner SQL only advances backoff time.
"""

import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.knowledge import KnowledgeStore, contract
from opspilot.knowledge.jobs import MAX_ATTEMPTS_PER_WATERMARK, GenerationStore
from opspilot.persistence import PoolConfig
from tests.f13_acceptance_support import seed_upstream

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)


@pytest.fixture
def stores():
    # Per-test scratch database: never alter or stop the lead's lab instance.
    lab = os.environ["OPSPILOT_LAB_DSN"]
    name = f"opspilot_f13view_{uuid4().hex[:12]}"
    with psycopg.connect(lab, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    dsn = make_conninfo(lab, dbname=name)
    knowledge = jobs = None
    try:
        schema.migrate(dsn, pg_dump=os.environ.get("OPSPILOT_PG_DUMP", "pg_dump"))
        pool = PoolConfig(min_size=0, max_size=3, timeout=5.0)
        knowledge = KnowledgeStore(dsn, pool=pool)
        jobs = GenerationStore(dsn, pool=pool)
        knowledge.install()
        jobs.install()
        yield knowledge, jobs, dsn
    finally:
        for store in (jobs, knowledge):
            if store is not None:
                store.close()
        with psycopg.connect(lab, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


def _versions(policy):
    return {
        "model": "deepseek-flash",
        "model_profile": contract.POSTMORTEM_MODEL_PROFILE,
        "prompt_version": contract.POSTMORTEM_PROMPT_VERSION,
        "output_schema_version": contract.POSTMORTEM_OUTPUT_SCHEMA_VERSION,
        "input_policy_version": policy,
    }


def _claim(jobs, incident, policy=None):
    return jobs.claim(
        incident,
        owner=uuid4(),
        versions=_versions(policy or contract.POSTMORTEM_INPUT_POLICY_VERSION),
        max_model_requests=2,
    )


def _fail(jobs, claim, code):
    jobs.finish(
        claim,
        status="failed",
        error_code=code,
        watermark=jobs.read_input(claim.incident_id)["watermark"],
    )


def _advance_backoff(dsn, incident):
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "UPDATE opspilot_postmortem_generation_jobs SET next_attempt_at="
            "clock_timestamp() - interval '1 second' WHERE incident_id=%s "
            "AND next_attempt_at IS NOT NULL",
            (incident,),
        )


def _historical_exhaustion(
    stores, code="INPUT_TOO_LARGE", policy="f13-postmortem-input-v2"
):
    knowledge, jobs, dsn = stores
    incident = seed_upstream(dsn)
    rounds = (
        MAX_ATTEMPTS_PER_WATERMARK
        if code in {"MODEL_UNAVAILABLE", "MODEL_REJECTED", "OUTPUT_INVALID"}
        else 1
    )
    for _ in range(rounds):
        claim = _claim(jobs, incident, policy)
        assert claim is not None, "historical fixture must be claimable"
        _fail(jobs, claim, code)
        _advance_backoff(dsn, incident)
    schedule = knowledge.incident_postmortem(incident)["schedule"]
    assert schedule["consecutive_failures"] == MAX_ATTEMPTS_PER_WATERMARK
    assert schedule["last_error_code"] == code
    assert schedule["next_attempt_at"] is None
    return incident


def test_d52_old_input_too_large_is_due_and_can_only_be_claimed_once(stores):
    knowledge, jobs, dsn = stores
    incident = _historical_exhaustion(stores)
    original_watermark = jobs.read_input(incident)["watermark"]
    assert incident in jobs.candidates(limit=100)
    assert incident in jobs.candidates(limit=100), "scan must not consume the new round"
    claim = _claim(jobs, incident)
    assert claim is not None
    assert _claim(jobs, incident) is None
    assert incident not in jobs.candidates(limit=100)
    assert jobs.read_input(incident)["watermark"] == original_watermark
    with psycopg.connect(dsn) as conn:
        attempts = conn.execute(
            "SELECT input_policy_version FROM opspilot_postmortem_generation_attempts "
            "WHERE incident_id=%s ORDER BY started_at",
            (incident,),
        ).fetchall()
    assert attempts == [
        ("f13-postmortem-input-v2",),
        (contract.POSTMORTEM_INPUT_POLICY_VERSION,),
    ]
    assert knowledge.incident_postmortem(incident)["status"] == "generating"


def test_d52_reopened_round_restarts_r7_and_has_its_own_cap(stores):
    knowledge, jobs, dsn = stores
    incident = _historical_exhaustion(stores)
    watermark = jobs.read_input(incident)["watermark"]
    for count in range(1, MAX_ATTEMPTS_PER_WATERMARK + 1):
        claim = _claim(jobs, incident)
        assert claim is not None, "old round's exhausted count must not carry forward"
        _fail(jobs, claim, "MODEL_UNAVAILABLE")
        schedule = knowledge.incident_postmortem(incident)["schedule"]
        assert schedule["consecutive_failures"] == count
        assert jobs.read_input(incident)["watermark"] == watermark
        if count < MAX_ATTEMPTS_PER_WATERMARK:
            assert schedule["next_attempt_at"] is not None
            assert _claim(jobs, incident) is None, "new round still respects backoff"
            _advance_backoff(dsn, incident)
        else:
            assert schedule["next_attempt_at"] is None
    assert incident not in jobs.candidates(limit=100)
    assert _claim(jobs, incident) is None


def test_d52_current_input_too_large_after_reopening_never_reopens_again(stores):
    knowledge, jobs, dsn = stores
    incident = _historical_exhaustion(stores)
    claim = _claim(jobs, incident)
    assert claim is not None
    _fail(jobs, claim, "INPUT_TOO_LARGE")
    schedule = knowledge.incident_postmortem(incident)["schedule"]
    assert schedule["consecutive_failures"] == MAX_ATTEMPTS_PER_WATERMARK
    assert schedule["next_attempt_at"] is None
    assert schedule["last_error_code"] == "INPUT_TOO_LARGE"
    # Fresh storage instances also must not reopen: no in-memory one-shot flag.
    jobs.close()
    fresh = GenerationStore(dsn, pool=PoolConfig(min_size=0, max_size=3, timeout=5.0))
    try:
        for _ in range(2):
            assert incident not in fresh.candidates(limit=100)
            assert _claim(fresh, incident) is None
    finally:
        fresh.close()
    assert len(knowledge.incident_postmortem(incident)["attempts"]) == 2


def test_d52_current_policy_exhaustion_without_history_stays_stopped(stores):
    _, jobs, _ = stores
    incident = _historical_exhaustion(
        stores, policy=contract.POSTMORTEM_INPUT_POLICY_VERSION
    )
    for _ in range(2):
        assert incident not in jobs.candidates(limit=100)
        assert _claim(jobs, incident) is None


@pytest.mark.parametrize(
    "code",
    [
        "MODEL_UNAVAILABLE",
        "MODEL_REJECTED",
        "OUTPUT_INVALID",
        "WATERMARK_MOVED",
        "GENERATION_CONFLICT",
    ],
)
def test_d52_other_exhausted_error_codes_do_not_reopen(stores, code):
    _, jobs, _ = stores
    incident = _historical_exhaustion(stores, code=code)
    for _ in range(2):
        assert incident not in jobs.candidates(limit=100)
        assert _claim(jobs, incident) is None
