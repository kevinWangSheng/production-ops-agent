"""#202 (M1-03 r10, D53-D56) on a real PostgreSQL.

Implementer tests: migration 0012 (old rows ``[]``, the array CHECK, the
worker's column grant, downgrade and back); every attempt outcome written by
the worker through its own login role with a scripted model (D54); and the
workbench page (web login) against the acceptance projection, both read
from ``KnowledgeStore.incident_postmortem`` (D56).
"""

import json
import os
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.acceptance import postmortem_outcome, postmortem_records
from opspilot.investigation.loop import ModelCall, ModelError, ModelReply
from opspilot.knowledge import KnowledgeStore
from opspilot.knowledge.generation import (
    MAX_MODEL_REQUESTS_PER_GENERATION,
    MAX_OUTPUT_TOKENS,
    PROBLEM_CODES,
    InputTooLarge,
)
from opspilot.knowledge.jobs import GenerationStore
from opspilot.knowledge.worker import PostmortemWorker
from opspilot.persistence.grants import WEB_ROLE, WORKER_ROLE
from scripts.m0.postgres_lab import DSN
from tests.f13_acceptance_support import (
    Harness,
    normalize,
    output,
    scenario,
    seed_upstream,
)
from tests.integration.test_runtime_roles_postgres import _workbench
from tests.runtime_roles_support import login

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

REASONING_TEXT = "private-chain-of-thought-must-not-be-stored"
ATTEMPTS = "opspilot_postmortem_generation_attempts"
ENTRY_KEYS = {
    "seq",
    "purpose",
    "finish_reason",
    "prompt_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "visible_completion_tokens",
    "outcome",
    "problems",
}


# --- D53: migration


def test_0012_adds_the_column_old_rows_read_unknown_and_downgrade_drops_it():
    name = f"opspilot_f13calls_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    scratch = make_conninfo(DSN, dbname=name)
    config = schema._config(scratch)
    try:
        schema.command.upgrade(config, "0011_runtime_roles")
        incident = seed_upstream(scratch)
        old = uuid4()
        with psycopg.connect(scratch) as conn:
            conn.execute(
                f"INSERT INTO {ATTEMPTS}(attempt_id, incident_id, lease_owner, "
                "lease_epoch, watermark, model, model_profile, prompt_version, "
                "output_schema_version, input_policy_version, max_model_requests) "
                "VALUES (%s, %s, %s, 1, '{}', 'm', 'p', 'v', 'o', 'i', 2)",
                (old, incident, uuid4()),
            )
        schema.command.upgrade(config, "head")
        with psycopg.connect(scratch) as conn:
            assert conn.execute(
                f"SELECT model_calls FROM {ATTEMPTS} WHERE attempt_id=%s", (old,)
            ).fetchone() == ([],)
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    f"UPDATE {ATTEMPTS} SET model_calls='{{}}' WHERE attempt_id=%s",
                    (old,),
                )
        with psycopg.connect(scratch) as conn:
            with pytest.raises(psycopg.errors.NotNullViolation):
                conn.execute(
                    f"UPDATE {ATTEMPTS} SET model_calls=NULL WHERE attempt_id=%s",
                    (old,),
                )
        with psycopg.connect(scratch) as conn:
            privileges = {
                (role, kind): conn.execute(
                    "SELECT has_column_privilege(%s, %s, 'model_calls', %s)",
                    (role, ATTEMPTS, kind),
                ).fetchone()[0]
                for role in (WEB_ROLE, WORKER_ROLE)
                for kind in ("SELECT", "UPDATE", "INSERT")
            }
        assert privileges == {
            (WEB_ROLE, "SELECT"): True,
            (WEB_ROLE, "UPDATE"): False,
            (WEB_ROLE, "INSERT"): False,
            (WORKER_ROLE, "SELECT"): True,
            (WORKER_ROLE, "UPDATE"): True,
            (WORKER_ROLE, "INSERT"): False,
        }
        schema.command.downgrade(config, "0011_runtime_roles")
        with psycopg.connect(scratch) as conn:
            assert not conn.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name=%s AND column_name='model_calls'",
                (ATTEMPTS,),
            ).fetchall()
            assert conn.execute(
                f"SELECT count(*) FROM {ATTEMPTS} WHERE attempt_id=%s", (old,)
            ).fetchone() == (1,)
        schema.command.upgrade(config, "head")
    finally:
        from opspilot.persistence import close_pools

        close_pools()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


# --- D54 / D56: the worker writes, the page and the projection read


class DiagnosedModel:
    """Replies in order with a scripted finish reason and usage; an
    exception is raised instead. Records every call."""

    def __init__(self, *steps) -> None:
        self.steps = list(steps)
        self.calls: list[ModelCall] = []

    def complete(self, call: ModelCall) -> ModelReply:
        self.calls.append(call)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        content, finish, usage = step
        return ModelReply(
            content=content if isinstance(content, str) else json.dumps(content),
            reasoning_content=REASONING_TEXT,
            tool_calls=(),
            finish_reason=finish,
            response_model="deepseek-flash",
            usage=usage,
            raw={},
        )


def _usage(completion: int, reasoning: int | None = None) -> dict:
    usage: dict = {"prompt_tokens": 1000, "completion_tokens": completion}
    if reasoning is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return usage


def _call(seq, finish, completion, reasoning, outcome, problems=()):
    return {
        "seq": seq,
        "purpose": "generate" if seq == 1 else "repair",
        "finish_reason": finish,
        "prompt_tokens": None if finish is None else 1000,
        "completion_tokens": completion,
        "reasoning_tokens": reasoning,
        "visible_completion_tokens": None
        if completion is None or reasoning is None
        else completion - reasoning,
        "outcome": outcome,
        "problems": list(problems),
    }


@pytest.fixture(scope="module")
def roles():
    with login(DSN, WEB_ROLE) as web, login(DSN, WORKER_ROLE) as worker:
        yield web, worker


@pytest.fixture
def harness(roles):
    """Pages, reviews and projection reads through the web login; generation
    through the worker login; only the upstream seed is written as owner."""
    web_dsn, worker_dsn = roles
    app, _, web_store, events = _workbench(web_dsn)
    knowledge = KnowledgeStore(worker_dsn)
    jobs = GenerationStore(worker_dsn)
    reads = KnowledgeStore(web_dsn)
    try:
        yield Harness(app, knowledge, jobs, web_store, DSN, events), reads
    finally:
        reads.close()
        jobs.close()
        knowledge.close()


def _stored(attempt_id: UUID) -> list:
    with psycopg.connect(DSN) as conn:
        return conn.execute(
            f"SELECT model_calls FROM {ATTEMPTS} WHERE attempt_id=%s", (attempt_id,)
        ).fetchone()[0]


def _shown(value) -> str:
    return "unknown" if value is None else str(value)


def _cell(calls) -> str:
    """What the page's per-request cell reads for ``calls``."""
    if not calls:
        return "unknown"
    return normalize(
        " ".join(
            f"{c['purpose']} · finish {_shown(c['finish_reason'])}"
            f" · prompt {_shown(c['prompt_tokens'])}"
            f" · completion {_shown(c['completion_tokens'])}"
            f" (reasoning {_shown(c['reasoning_tokens'])},"
            f" visible {_shown(c['visible_completion_tokens'])})"
            f" · {c['outcome']}"
            + (f" · {' '.join(c['problems'])}" if c["problems"] else "")
            for c in calls
        )
    )


def _check(harness, incident: UUID, expected: list[dict]) -> None:
    """Row, view, projection and page carry the same ``model_calls``."""
    h, reads = harness
    view = reads.incident_postmortem(incident)
    attempt = view["attempts"][0]
    assert attempt["model_calls"] == expected
    assert _stored(attempt["attempt_id"]) == expected
    for entry in expected:
        assert set(entry) == ENTRY_KEYS
        assert set(entry["problems"]) <= PROBLEM_CODES
    assert REASONING_TEXT not in json.dumps(expected)
    result = postmortem_outcome(scenario(incident), postmortem_records(reads, incident))
    assert result.unknown_reasons == ()
    assert [a["model_calls"] for a in result.recent_attempts] == [
        a["model_calls"] for a in view["attempts"]
    ]
    rows = h.page(f"/incidents/{incident}").rows["postmortem-attempts"]
    assert len(rows) == len(result.recent_attempts)
    for cells, a in zip(rows, result.recent_attempts, strict=True):
        assert cells[0] == str(a["attempt_id"])
        assert cells[-1] == _cell(a["model_calls"])


def test_first_reply_accepted(harness) -> None:
    h, _ = harness
    incident, ev, _, _ = h.seed()
    model = DiagnosedModel((output(ev), "stop", _usage(900, 700)))
    h.generate(incident, model)
    _check(harness, incident, [_call(1, "stop", 900, 700, "accepted")])
    # behavior unchanged: one request at the frozen cap
    assert [c.max_tokens for c in model.calls] == [MAX_OUTPUT_TOKENS]


def test_length_truncated_reply_is_repaired_then_accepted(harness) -> None:
    h, _ = harness
    incident, ev, _, _ = h.seed()
    model = DiagnosedModel(
        ('{"narrative_sections": [{"key": "imp', "length", _usage(16384, 16000)),
        (output(ev), "stop", _usage(5121)),
    )
    view = h.generate(incident, model)
    assert view["status"] == "under_review"
    _check(
        harness,
        incident,
        [
            _call(1, "length", 16384, 16000, "repair_requested", ["NOT_JSON"]),
            _call(2, "stop", 5121, None, "accepted"),
        ],
    )
    assert len(model.calls) == MAX_MODEL_REQUESTS_PER_GENERATION
    assert [c.max_tokens for c in model.calls] == [MAX_OUTPUT_TOKENS] * 2


def test_still_invalid_after_the_repair_is_rejected(harness) -> None:
    h, _ = harness
    incident, ev, _, _ = h.seed()
    bad = {"narrative_sections": [], "conclusions": [], "proposals": [], "disputes": []}
    model = DiagnosedModel(
        ("", "length", _usage(16384, 16384)),
        (bad, "stop", _usage(40, 10)),
    )
    view = h.generate(incident, model)
    assert view["status"] == "generation_failed"
    assert view["attempts"][0]["error_code"] == "OUTPUT_INVALID"
    _check(
        harness,
        incident,
        [
            _call(1, "length", 16384, 16384, "repair_requested", ["EMPTY_REPLY"]),
            _call(
                2,
                "stop",
                40,
                10,
                "rejected",
                ["narrative_sections.impact_summary_missing"],
            ),
        ],
    )


def test_citation_failed_draft_is_the_accepted_reply(harness) -> None:
    h, _ = harness
    incident, ev, _, _ = h.seed()
    bad = output(ev, citation_failure=True)
    model = DiagnosedModel((bad, "stop", _usage(300)), (bad, "stop", _usage(310)))
    view = h.generate(incident, model)
    assert view["status"] == "citations_failed"
    cited = ["citations.UNKNOWN_EVIDENCE", "citations.NOT_CITABLE_AS_FACT"]
    _check(
        harness,
        incident,
        [
            _call(1, "stop", 300, None, "repair_requested", cited),
            _call(2, "stop", 310, None, "accepted", cited),
        ],
    )


@pytest.mark.parametrize("code", ["MODEL_UNAVAILABLE", "MODEL_REJECTED"])
def test_provider_failure_on_the_first_request(harness, code) -> None:
    h, _ = harness
    incident, _, _, _ = h.seed()
    view = h.generate(incident, DiagnosedModel(ModelError(code)))
    assert view["attempts"][0]["error_code"] == code
    _check(harness, incident, [_call(1, None, None, None, "provider_error")])


def test_provider_failure_on_the_repair_request(harness) -> None:
    h, _ = harness
    incident, _, _, _ = h.seed()
    model = DiagnosedModel(
        ("not json", "stop", _usage(50, 20)), ModelError("MODEL_UNAVAILABLE")
    )
    view = h.generate(incident, model)
    assert view["attempts"][0]["error_code"] == "MODEL_UNAVAILABLE"
    _check(
        harness,
        incident,
        [
            _call(1, "stop", 50, 20, "repair_requested", ["NOT_JSON"]),
            _call(2, None, None, None, "provider_error"),
        ],
    )


def test_input_over_a_limit_sends_nothing_and_reads_unknown(harness) -> None:
    h, _ = harness
    incident, _, _, _ = h.seed()

    def too_large(raw):
        raise InputTooLarge("MAX_INPUT_BYTES")

    model = DiagnosedModel()
    PostmortemWorker(
        knowledge=h.knowledge, jobs=h.jobs, model=model, input_builder=too_large
    ).generate(incident)
    assert model.calls == []
    _check(harness, incident, [])
