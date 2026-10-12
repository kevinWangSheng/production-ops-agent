"""Independent #202 r10 D53--D56 on a scratch DB, never the lab's rows.

Uses the existing local cluster; no initdb/pg_ctl or real model/network.
Only downgrade 0012 -> 0011, leaving cluster-global runtime roles intact.
"""

import os
import re
from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from opspilot import schema
from opspilot.acceptance import postmortem_outcome, postmortem_records
from opspilot.knowledge import KnowledgeStore
from opspilot.knowledge.generation import build_input
from opspilot.knowledge.jobs import GenerationStore
from opspilot.knowledge.worker import PostmortemWorker
from opspilot.persistence import PoolConfig, close_pools
from opspilot.persistence.grants import RUNTIME_GRANTS
from scripts.m0.postgres_lab import DSN
from tests.acceptance.test_f13_postmortem import h  # noqa: F401
from tests.f13_acceptance_support import scenario
from tests.integration.test_m1_03_draft_contract_postgres import (
    _citable_evidence,
    _ended_incident,
)
from tests.runtime_roles_support import catalog_grants, login
from tests.test_f13_call_diagnostics_contract import (
    BODY,
    PROMPT,
    REASONING,
    ScriptedModel,
    reply,
    valid_output,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)
TABLE = "opspilot_postmortem_generation_attempts"
POOL = PoolConfig(min_size=0, max_size=2, timeout=5)


@pytest.fixture
def acceptance_h(request):
    return request.getfixturevalue("h")


def config(dsn):
    value = Config(attributes={"dsn": dsn})
    value.set_main_option(
        "script_location", str(schema.__file__.replace("schema.py", "migrations"))
    )
    return value


@contextmanager
def database():
    name = f"opspilot_f13_calls_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    scratch = make_conninfo(DSN, dbname=name)
    try:
        schema.migrate(scratch, pg_dump=os.environ.get("OPSPILOT_PG_DUMP", "pg_dump"))
        yield scratch
    finally:
        close_pools()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture(scope="module")
def dsn():
    with database() as scratch:
        yield scratch


def column(conn):
    return conn.execute(
        "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s AND column_name='model_calls'",
        (TABLE,),
    ).fetchone()


def legacy_attempt(dsn):
    """Insert a pre-0012 attempt using only columns that existed in 0011."""
    incident = _ended_incident(dsn)
    attempt = uuid4()
    owner = uuid4()
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            """SELECT i.control_generation, i.observation_generation,
                    i.current_run_id,
                    (SELECT count(*) FROM opspilot_runs r WHERE r.incident_id=i.incident_id),
                    (SELECT coalesce(max(n.sequence),0) FROM opspilot_inputs n WHERE n.incident_id=i.incident_id),
                    s.session_id, e.ending_id
             FROM opspilot_incidents i
             JOIN opspilot_observation_sessions s ON s.incident_id=i.incident_id
               AND s.observation_generation=i.observation_generation AND s.state <> 'authorized'
             JOIN opspilot_observation_endings e ON e.session_id=s.session_id
             WHERE i.incident_id=%s""",
            (incident,),
        ).fetchone()
        assert row is not None
        watermark = {
            "incident_control_generation": row[0],
            "observation_generation": row[1],
            "observation_session_id": str(row[5]),
            "observation_ending_id": str(row[6]),
            "run_count": row[3],
            "last_run_id": None if row[2] is None else str(row[2]),
            "input_watermark": row[4],
            "evidence_snapshot_sha256": "0" * 64,
        }
        conn.execute(
            "INSERT INTO opspilot_postmortem_generation_attempts "
            "(attempt_id, incident_id, lease_owner, lease_epoch, status, error_code, watermark, "
            "model, response_model, model_profile, prompt_version, output_schema_version, "
            "input_policy_version, max_model_requests, model_requests, finished_at) "
            "VALUES (%s,%s,%s,0,'failed','MODEL_UNAVAILABLE',%s,'deepseek-flash',NULL,"
            "'deepseek-flash-v1','f13-postmortem-prompt-v4','f13-postmortem-output-v2',"
            "'f13-postmortem-input-v3',2,0,clock_timestamp())",
            (attempt, incident, owner, Jsonb(watermark)),
        )
    return incident, attempt


def test_d53_migration_old_rows_check_and_roundtrip():
    with database() as scratch:
        command.downgrade(config(scratch), "0011_runtime_roles")
        with psycopg.connect(scratch) as conn:
            assert column(conn) is None
        _, attempt = legacy_attempt(scratch)
        command.upgrade(config(scratch), "head")
        with psycopg.connect(scratch) as conn:
            info = column(conn)
            assert info is not None, "D53: head must add model_calls"
            assert info[:2] == ("jsonb", "NO")
            assert "[]" in info[2]
            assert conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone() == (schema.head_revision(),)
            assert conn.execute(
                f"SELECT model_calls FROM {TABLE} WHERE attempt_id=%s", (attempt,)
            ).fetchone() == ([],)
            for bad in ({}, "text", 1, None):
                with pytest.raises(psycopg.errors.CheckViolation):
                    with conn.transaction():
                        conn.execute(
                            f"UPDATE {TABLE} SET model_calls=%s WHERE attempt_id=%s",
                            (Jsonb(bad), attempt),
                        )
            with pytest.raises(psycopg.errors.NotNullViolation):
                with conn.transaction():
                    conn.execute(
                        f"UPDATE {TABLE} SET model_calls=NULL WHERE attempt_id=%s",
                        (attempt,),
                    )
        command.downgrade(config(scratch), "0011_runtime_roles")
        with psycopg.connect(scratch) as conn:
            assert column(conn) is None
            assert conn.execute(
                f"SELECT attempt_id FROM {TABLE} WHERE attempt_id=%s", (attempt,)
            ).fetchone() == (attempt,)
        command.upgrade(config(scratch), "head")
        with psycopg.connect(scratch) as conn:
            assert column(conn) is not None
            assert conn.execute(
                f"SELECT model_calls FROM {TABLE} WHERE attempt_id=%s", (attempt,)
            ).fetchone() == ([],)


def test_d53_runtime_grants_match_catalog_and_column_permissions(dsn):
    with psycopg.connect(dsn) as conn:
        assert column(conn) is not None, "D53: model_calls column missing"
        for role in ("opspilot_worker", "opspilot_web"):
            assert catalog_grants(conn, role) == RUNTIME_GRANTS[role]
            assert conn.execute(
                "SELECT has_column_privilege(%s,%s,'model_calls','SELECT')",
                (role, TABLE),
            ).fetchone() == (True,)
        assert conn.execute(
            "SELECT has_column_privilege('opspilot_worker',%s,'model_calls','UPDATE')",
            (TABLE,),
        ).fetchone() == (True,)
        for permission in ("UPDATE", "INSERT", "REFERENCES"):
            assert conn.execute(
                "SELECT has_column_privilege('opspilot_web',%s,'model_calls',%s)",
                (TABLE, permission),
            ).fetchone() == (False,)


def test_d54_worker_login_finishes_and_persists_fake_client_diagnostics(dsn):
    incident = _ended_incident(dsn)
    import json

    evidence_id = _citable_evidence(dsn, incident)
    with login(dsn, "opspilot_worker") as runtime:
        with psycopg.connect(runtime) as conn:
            assert conn.execute(
                "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
            ).fetchone() == (False,)
        jobs, knowledge = (
            GenerationStore(runtime, pool=POOL),
            KnowledgeStore(runtime, pool=POOL),
        )
        try:
            built = build_input(jobs.read_input(incident), secrets=[])
            catalog_entry = next(
                (
                    entry
                    for entry in built.catalog
                    if entry["evidence_id"] == evidence_id
                ),
                None,
            )
            assert catalog_entry is not None
            assert catalog_entry["citable_as_fact"] is True
            model = ScriptedModel([reply(json.dumps(valid_output(evidence_id)))])
            result = PostmortemWorker(
                knowledge=knowledge, jobs=jobs, model=model
            ).generate(incident)
            assert result is not None and result.status == "succeeded"
        finally:
            jobs.close()
            knowledge.close()
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            f"SELECT to_jsonb(a) FROM {TABLE} a WHERE attempt_id=%s",
            (result.attempt_id,),
        ).fetchone()[0]
    assert row["status"] == "succeeded" and len(model.calls) == 1
    assert "model_calls" in row, "D54: persisted attempt lacks diagnostics"
    assert row["model_calls"] == [
        {
            "seq": 1,
            "purpose": "generate",
            "finish_reason": "stop",
            "prompt_tokens": 101,
            "completion_tokens": 83,
            "reasoning_tokens": 59,
            "visible_completion_tokens": 24,
            "outcome": "accepted",
            "problems": [],
        }
    ]
    for private in (REASONING, BODY, PROMPT, "reasoning_content"):
        assert private not in json.dumps(row["model_calls"])


def attempt_projection(acceptance_h, incident):
    outcome = postmortem_outcome(
        scenario(incident), postmortem_records(acceptance_h.knowledge, incident)
    )
    assert outcome.unknown_reasons == ()
    assert len(outcome.recent_attempts) == 1
    return outcome.recent_attempts[0]


@pytest.mark.parametrize("invalid", [False, True])
def test_d56_projection_and_incident_page_show_same_per_request_diagnostics(
    acceptance_h, invalid
):
    import json

    incident, evidence, _, _ = acceptance_h.seed()
    model = ScriptedModel(
        [
            reply("{", finish_reason="length"),
            reply("not json " + BODY)
            if invalid
            else reply(json.dumps(valid_output(evidence))),
        ]
    )
    acceptance_h.generate(incident, model)
    projected = attempt_projection(acceptance_h, incident)
    with psycopg.connect(acceptance_h.dsn) as conn:
        persisted = conn.execute(
            f"SELECT to_jsonb(a) FROM {TABLE} a WHERE attempt_id=%s",
            (projected["attempt_id"],),
        ).fetchone()[0]
    assert "model_calls" in projected, "D56: acceptance history lacks model_calls"
    calls = projected["model_calls"]
    assert calls == persisted["model_calls"]
    assert [c["finish_reason"] for c in calls] == ["length", "stop"]
    assert [c["outcome"] for c in calls] == [
        "repair_requested",
        "rejected" if invalid else "accepted",
    ]
    text = acceptance_h.page(f"/incidents/{incident}").text("postmortem-attempts")
    for call in calls:
        for key in ("finish_reason", "outcome"):
            assert call[key] in text, (key, text)
        if call["problems"]:
            assert "problems" in text or "problem" in text.lower()
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "visible_completion_tokens",
        ):
            assert re.search(rf"(?<!\d){call[key]}(?!\d)", text), (key, text)
        assert (
            call["prompt_tokens"],
            call["completion_tokens"],
            call["reasoning_tokens"],
            call["visible_completion_tokens"],
        ) == (101, 83, 59, 24)
    assert text.index("length") < text.index("stop")
    assert REASONING not in text


def test_d56_old_attempt_is_unknown_in_projection_and_page(acceptance_h):
    # Use a real attempt made without diagnostics; default [] is the legacy
    # representation. The migration test separately proves pre-0012 rows.
    incident, attempt = legacy_attempt(acceptance_h.dsn)
    projected = attempt_projection(acceptance_h, incident)
    assert projected["attempt_id"] == attempt
    assert "model_calls" in projected, (
        "D56: legacy history lacks unknown representation"
    )
    assert projected["model_calls"] == []
    text = acceptance_h.page(f"/incidents/{incident}").text("postmortem-attempts")
    assert "未知" in text or "unknown" in text.lower(), text
    assert "accepted" not in text and "provider_error" not in text
