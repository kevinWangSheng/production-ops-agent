"""Independent r12 item 5: a job capped by INPUT_TOO_LARGE under the v3 input
policy is reopened once under v4 by the existing D52 rule, and a second
INPUT_TOO_LARGE under v4 stops it for good.

Only storage public operations run; no worker/model/network calls. Owner SQL
only advances backoff time and reads recorded attempts.
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
from scripts.m0.postgres_lab import DSN
from tests.f13_acceptance_support import seed_upstream

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

OLD_POLICY = "f13-postmortem-input-v3"
NEW_POLICY = "f13-postmortem-input-v4"


@pytest.fixture
def stores():
    # Per-test scratch database: never alter or stop the lead's lab instance.
    lab = DSN
    name = f"opspilot_f13r12_{uuid4().hex[:12]}"
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


def _attempt_policies(dsn, incident):
    with psycopg.connect(dsn) as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT input_policy_version FROM "
                "opspilot_postmortem_generation_attempts WHERE incident_id=%s "
                "ORDER BY started_at",
                (incident,),
            ).fetchall()
        ]


def _v3_input_too_large(stores):
    knowledge, jobs, dsn = stores
    incident = seed_upstream(dsn)
    claim = _claim(jobs, incident, OLD_POLICY)
    assert claim is not None, "historical fixture must be claimable"
    _fail(jobs, claim, "INPUT_TOO_LARGE")
    schedule = knowledge.incident_postmortem(incident)["schedule"]
    assert schedule["consecutive_failures"] == MAX_ATTEMPTS_PER_WATERMARK
    assert schedule["last_error_code"] == "INPUT_TOO_LARGE"
    assert schedule["next_attempt_at"] is None
    return incident


def test_r12_current_input_policy_is_v4():
    assert contract.POSTMORTEM_INPUT_POLICY_VERSION == NEW_POLICY


def test_r12_v3_input_too_large_reopens_under_v4_with_reset_count(stores):
    knowledge, jobs, dsn = stores
    assert contract.POSTMORTEM_INPUT_POLICY_VERSION == NEW_POLICY
    incident = _v3_input_too_large(stores)
    watermark = jobs.read_input(incident)["watermark"]
    assert incident in jobs.candidates(limit=100)
    claim = _claim(jobs, incident)
    assert claim is not None
    assert _claim(jobs, incident) is None
    assert jobs.read_input(incident)["watermark"] == watermark
    # The v4 round starts its own R7 count: one ordinary failure counts as 1.
    _fail(jobs, claim, "MODEL_UNAVAILABLE")
    schedule = knowledge.incident_postmortem(incident)["schedule"]
    assert schedule["consecutive_failures"] == 1
    assert schedule["next_attempt_at"] is not None
    assert _attempt_policies(dsn, incident) == [OLD_POLICY, NEW_POLICY]


def test_r12_second_input_too_large_under_v4_caps_for_good(stores):
    knowledge, jobs, dsn = stores
    assert contract.POSTMORTEM_INPUT_POLICY_VERSION == NEW_POLICY
    incident = _v3_input_too_large(stores)
    claim = _claim(jobs, incident)
    assert claim is not None
    _fail(jobs, claim, "INPUT_TOO_LARGE")
    schedule = knowledge.incident_postmortem(incident)["schedule"]
    assert schedule["consecutive_failures"] == MAX_ATTEMPTS_PER_WATERMARK
    assert schedule["next_attempt_at"] is None
    assert schedule["last_error_code"] == "INPUT_TOO_LARGE"
    jobs.close()
    fresh = GenerationStore(dsn, pool=PoolConfig(min_size=0, max_size=3, timeout=5.0))
    try:
        for _ in range(2):
            assert incident not in fresh.candidates(limit=100)
            assert _claim(fresh, incident) is None
    finally:
        fresh.close()
    assert _attempt_policies(dsn, incident) == [OLD_POLICY, NEW_POLICY]
