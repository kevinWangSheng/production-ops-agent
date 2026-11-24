"""Reviewed per-target standing operator instructions as a model input
(#780) on the product path, PostgreSQL.

Independent acceptance and contract tests (the author did not see the
implementation), from open-issues round R10, pre-review A section #780
(defaults 780-1..780-11 binding), B4-U3=a / B4-U4=a and B4-9..B4-12:

* lifecycle -- a ``standing_instructions`` submission goes through the
  runbook submit -> approve / reject -> revoke flow; only an approved,
  unrevoked revision reaches a Run (B4-U3 a);
* one per target -- a second distinct entry for a target with one is
  refused ``STANDING_INSTRUCTIONS_EXISTS``; superseding the fixed-name
  ``standing-instructions`` entry works (780-2); the body is 1-4096 bytes
  (B4-9);
* Run input -- ``opspilot-investigation-input-v9`` with the fact
  ``standing_instructions`` (entry, revision, approver, approval instant,
  redacted body) (780-5);
* model input -- with instructions, one separate user message before the
  runbook catalog whose boundary wording counts in ``prompt_revision``;
  without approved instructions the input, the model messages and
  ``prompt_revision`` are what they were before #780, with
  ``OPSPILOT_KNOWLEDGE_TOOL=1`` (B4-9, B4-10, 780-3, 780-4);
* successor Runs re-read at creation; a revoke after a Run was created does
  not change that Run (780-7, 必答 1);
* migration 0029 -- rows written before it read as kind ``runbook`` (780-1).

The tests that run the worker are in
``test_780_standing_instructions_runs_postgres.py`` (one database each).

The credential-redaction and F7 step 2 halves are in
``tests/safety/test_safety_model_inputs_postgres.py``.

Harness: ``tests/runbook_fetch_support.py`` (extension profile, switch on,
real web, runner and executor, worker login, scripted provider).
ASSUMPTION: the web reads standing instructions through the same
``runbook_catalog=`` source it is handed (the ``RunbookStore``); adapting
that wiring is not an assertion change. Names: see
``tests/standing_instructions_support.py``. Run with
``M1_DURABLE_POSTGRES=1``.
"""

from __future__ import annotations

import os
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from opspilot.investigation.context import (
    RUNBOOK_CATALOG_BOUNDARY,
    InvestigationInput,
)
from opspilot.persistence.grants import WORKER_ROLE
from opspilot.tools.registry import canonical
from tests.acceptance.f7_pg_support import ScriptedProvider, assistant
from tests.runbook_fetch_support import (
    FetchHarness,
    build_harness,
    report_round,
    traces,
)
from tests.runbook_support import REVIEWER, scratch_database
from tests.runtime_roles_support import login
from tests.standing_instructions_support import (
    EXISTS,
    FACT,
    KIND,
    V7,
    V9,
    body_of,
    code_of,
    si_publish,
    si_submit,
    si_submitted,
    standing,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)

QUESTION = "Why is checkout erroring?"
U = uuid4().hex[:6]


def _target(label: str) -> str:
    return f"si-{label}-{U}"


def _body(label: str) -> str:
    return (
        f"* Always check pod restarts first. si-body-{label}-{U}\n"
        f"* Report the checkout error rate. si-check-{label}-{U}\n"
    )


def _instant(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    assert isinstance(value, str), value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _same_instant(left: object, right: object) -> bool:
    return abs(_instant(left) - _instant(right)) < timedelta(seconds=1)


@pytest.fixture(scope="module")
def ca_file(tmp_path_factory):
    path = tmp_path_factory.mktemp("si-ca") / "synthetic-ca-placeholder.pem"
    path.write_text("synthetic ca placeholder\n")
    return path


@contextmanager
def _harness(ca_file, prefix: str):
    with ExitStack() as stack:
        patch = stack.enter_context(pytest.MonkeyPatch.context())
        dsn = stack.enter_context(scratch_database(prefix))
        worker_dsn = stack.enter_context(login(dsn, WORKER_ROLE))
        harness = build_harness(dsn, worker_dsn, patch, ca_file)
        stack.callback(harness.close)
        yield harness


@pytest.fixture(scope="module")
def h(ca_file):
    """Shared scratch database for tests that only read recorded inputs
    (each on its own target, so the one-per-target rule never collides)."""
    with _harness(ca_file, "opspilot_780_si") as harness:
        yield harness


def _run_once(h: FetchHarness, incident):
    """One worker attempt: a traces read, then a report citing it."""
    provider = ScriptedProvider([assistant(tool_calls=[traces()]), report_round()])
    outcome = h.run(incident, provider)
    assert provider.requests, outcome
    return outcome, provider


def _revision_of(outcome) -> str:
    loop = getattr(outcome, "loop", None)
    assert loop is not None, outcome
    return loop.prompt_revision


def _kinds(h: FetchHarness, table: str, column: str, value: str) -> list[str]:
    return [
        r["kind"]
        for r in h.rows(
            f"SELECT kind FROM {table} WHERE {column}::text = %s",  # noqa: S608
            (value,),
        )
    ]


def _refused_as_existing(h: FetchHarness, response, *, submission_id=None) -> None:
    """780-2: the service refuses a second entry for the target, either when
    it is submitted or, at the latest, when it would be approved."""
    if response is not None and response.status != 200:
        assert 400 <= response.status < 500, response.text
        assert code_of(response) == EXISTS, response.text
        return
    sid = submission_id or response.json()["submission_id"]
    approved = h.rb_review(sid)
    assert 400 <= approved.status < 500, approved.text
    assert code_of(approved) == EXISTS, approved.text


def _active(h: FetchHarness, target: str) -> list[dict[str, Any]]:
    return h.rows(
        "SELECT r.entry_id, r.revision FROM opspilot_runbook_revisions r "
        "JOIN opspilot_runbook_revision_states s "
        "ON s.entry_id = r.entry_id AND s.revision = r.revision "
        "WHERE s.state = 'active' AND r.resource_uid = %s AND r.kind = %s",
        (target, KIND),
    )


# -- lifecycle: submit -> approve / reject -> revoke (B4-U3 a) --------------------


def test_only_an_approved_unrevoked_revision_reaches_the_run_input(h):
    target = _target("life")
    body = _body("life")
    sid = si_submitted(h, body, resource_uid=target)
    assert _kinds(h, "opspilot_runbook_submissions", "submission_id", sid) == [KIND]

    # Pending: not in a Run.
    pending, _ = h.submit(QUESTION, target_id=target)
    recorded = h.recorded(pending)
    assert FACT not in recorded["scope_facts"]
    assert recorded["version"] == V7
    assert body not in canonical(recorded)

    # Approved: in the next Run, with its provenance (780-5).
    approved = h.rb_review(sid)
    assert approved.status == 200, approved.text
    result = approved.json()
    entry_id, revision = result["entry_id"], result["revision"]
    assert revision == 1
    assert _kinds(h, "opspilot_runbook_revisions", "entry_id", entry_id) == [KIND]
    incident, _ = h.submit(QUESTION, target_id=target)
    recorded = h.recorded(incident)
    assert recorded["version"] == V9
    InvestigationInput.from_json(recorded)  # the v9 snapshot validates
    fact = recorded["scope_facts"][FACT]
    assert str(fact["entry_id"]) == str(entry_id)
    assert fact["revision"] == 1
    assert fact["approved_by"] == REVIEWER
    db = h.revision_row(entry_id, 1)
    assert db["approved_by"] == REVIEWER
    assert _same_instant(fact["approved_at"], db["approved_at"])
    assert body_of(fact) == body  # nothing credential-shaped to redact here

    # Revoked: gone from the next Run.
    h.rb_revoke(entry_id, 1)
    after, _ = h.submit(QUESTION, target_id=target)
    recorded = h.recorded(after)
    assert FACT not in recorded["scope_facts"]
    assert recorded["version"] == V7


def test_a_rejected_submission_never_reaches_a_run(h):
    target = _target("rej")
    body = _body("rej")
    sid = si_submitted(h, body, resource_uid=target)
    rejected = h.rb_review(sid, "reject")
    assert rejected.status == 200, rejected.text
    incident, _ = h.submit(QUESTION, target_id=target)
    recorded = h.recorded(incident)
    assert FACT not in recorded["scope_facts"]
    assert body not in canonical(recorded)


def test_a_submission_without_kind_stays_a_runbook(h):
    target = _target("plain")
    published = h.rb_publish(resource_uid=target, name=f"plain-{U}")
    assert _kinds(
        h, "opspilot_runbook_submissions", "submission_id", published["submission_id"]
    ) == ["runbook"]
    assert _kinds(
        h, "opspilot_runbook_revisions", "entry_id", published["entry_id"]
    ) == ["runbook"]
    incident, _ = h.submit(QUESTION, target_id=target)
    recorded = h.recorded(incident)
    assert FACT not in recorded["scope_facts"]
    assert recorded["version"] == V7


@pytest.mark.parametrize("kind", ["standing", "Standing_Instructions", "global"])
def test_an_unknown_kind_is_refused_and_writes_nothing(h, kind):
    before = h.runbook_counts()
    response = si_submit(h, _body("kind"), resource_uid=_target("kind"), kind=kind)
    assert response.status == 400, response.text
    assert h.runbook_counts() == before


# -- at most one active per target; supersede (780-2) ----------------------------


def test_a_second_entry_for_a_target_with_one_is_refused(h):
    target = _target("dup")
    first = si_publish(h, _body("dup-1"), resource_uid=target)
    response = si_submit(h, _body("dup-2"), resource_uid=target)
    _refused_as_existing(h, response)
    assert [str(r["entry_id"]) for r in _active(h, target)] == [str(first["entry_id"])]
    incident, _ = h.submit(QUESTION, target_id=target)
    assert body_of(standing(h, incident)) == _body("dup-1")


def test_two_pending_entries_for_one_target_never_both_become_active(h):
    target = _target("race")
    first = si_submitted(h, _body("race-1"), resource_uid=target)
    second = si_submit(h, _body("race-2"), resource_uid=target)
    approved = h.rb_review(first)
    assert approved.status == 200, approved.text
    _refused_as_existing(h, second)
    assert len(_active(h, target)) == 1


def test_another_target_keeps_its_own_instructions(h):
    mine, other = _target("own-a"), _target("own-b")
    si_publish(h, _body("own-a"), resource_uid=mine)
    si_publish(h, _body("own-b"), resource_uid=other)  # not refused: other target
    incident, _ = h.submit(QUESTION, target_id=mine)
    recorded = canonical(h.recorded(incident))
    assert _body("own-a") in recorded
    assert _body("own-b") not in recorded


def test_superseding_the_fixed_name_entry_moves_new_runs_to_the_new_revision(h):
    target = _target("super")
    first = si_publish(h, _body("super-1"), resource_uid=target)
    sid = si_submitted(
        h,
        _body("super-2"),
        resource_uid=target,
        supersedes_entry_id=first["entry_id"],
    )
    approved = h.rb_review(sid)
    assert approved.status == 200, approved.text
    assert approved.json()["revision"] == 2
    assert str(approved.json()["entry_id"]) == str(first["entry_id"])
    incident, _ = h.submit(QUESTION, target_id=target)
    fact = standing(h, incident)
    assert fact is not None
    assert (str(fact["entry_id"]), fact["revision"]) == (str(first["entry_id"]), 2)
    assert body_of(fact) == _body("super-2")
    assert _body("super-1") not in canonical(h.recorded(incident))


# -- 1-4096 bytes of plain text (B4-9) --------------------------------------------

TOO_LONG = {
    "empty": "",
    "ascii-4097": "a" * 4097,
    # 2049 characters, 4097 bytes: the limit is in bytes, not characters.
    "multibyte-4097": "é" * 2048 + "a",
}


@pytest.mark.parametrize("case", sorted(TOO_LONG))
def test_a_body_outside_one_to_4096_bytes_is_refused_and_writes_nothing(h, case):
    before = h.runbook_counts()
    response = si_submit(h, TOO_LONG[case], resource_uid=_target(f"len-{case}"))
    assert response.status == 400, response.text
    assert h.runbook_counts() == before


def test_a_4096_byte_body_is_accepted_and_reaches_the_run_whole(h):
    target = _target("len-max")
    body = "é" * 2048  # 4096 bytes in UTF-8
    assert len(body.encode("utf-8")) == 4096
    si_publish(h, body, resource_uid=target)
    incident, _ = h.submit(QUESTION, target_id=target)
    assert body_of(standing(h, incident)) == body


def test_a_runbook_keeps_its_own_larger_limit(h):
    published = h.rb_publish(
        resource_uid=_target("len-rb"), name=f"len-rb-{U}", content="b" * 5000
    )
    assert published["entry_id"]


# -- the model input (B4-9, B4-10, 780-3, 780-4) ---------------------------------------


def _catalog_index(messages: list[dict[str, Any]]) -> int:
    found = [
        i
        for i, m in enumerate(messages)
        if isinstance(m.get("content"), str)
        and m["content"].startswith(RUNBOOK_CATALOG_BOUNDARY)
    ]
    assert len(found) == 1, found
    return found[0]


# -- snapshot at creation; successor Runs re-read (780-7) ------------------------------


def test_a_switch_off_web_neither_reads_nor_carries_instructions(h):
    target = _target("off")
    si_publish(h, _body("off"), resource_uid=target)
    off = h.workbench_app(catalog=None, switch="0")
    incident, _ = h.submit(QUESTION, target_id=target, app=off)
    recorded = h.recorded(incident)
    assert FACT not in recorded["scope_facts"]
    assert recorded["version"] != V9
    assert _body("off") not in canonical(recorded)

    on_incident, _ = h.submit(QUESTION, target_id=target)
    assert standing(h, on_incident) is not None
    assert h.control(on_incident, "cancel").status == 200
    again = h.control(on_incident, "new_run", app=off)
    assert again.status == 200, again.text
    dropped = h.store.rebuild(on_incident)["run"]
    assert FACT not in dropped["input"]["scope_facts"]
    assert dropped["input"]["version"] != V9


# -- migration 0029: old rows are runbooks (780-1) ----------------------------------------


def _standing_revision() -> tuple[str, str]:
    """The migration after ``*_runbook_imports`` (``0029`` as of the
    pre-review) and its down revision, found by ancestry, not by number."""
    from alembic.script import ScriptDirectory

    from opspilot import schema
    from tests.runbook_support import runbook_revision

    imports, _ = runbook_revision()
    script_dir = ScriptDirectory.from_config(schema._config(""))
    children = [s for s in script_dir.walk_revisions() if s.down_revision == imports]
    assert len(children) == 1, [s.revision for s in children]
    return children[0].revision, imports


def test_rows_written_before_the_kind_column_read_as_runbooks():
    from opspilot import schema
    from tests.runbook_support import Harness

    revision, down = _standing_revision()
    assert revision.startswith("0029"), revision
    with scratch_database("opspilot_780_mig") as dsn:
        harness = Harness(dsn)
        try:
            submission_id, approved = harness.publish()
        finally:
            harness.close()
        schema.command.downgrade(schema._config(dsn), down)
        with psycopg.connect(dsn) as conn:
            columns = {
                r[0]
                for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'opspilot_runbook_revisions'"
                )
            }
        assert "kind" not in columns
        schema.command.upgrade(schema._config(dsn), "head")
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            for table, column, value in (
                ("opspilot_runbook_submissions", "submission_id", submission_id),
                ("opspilot_runbook_revisions", "entry_id", approved["entry_id"]),
            ):
                rows = conn.execute(
                    f"SELECT kind FROM {table} WHERE {column}::text = %s",  # noqa: S608
                    (str(value),),
                ).fetchall()
                assert [r["kind"] for r in rows] == ["runbook"], table
            for table in ("opspilot_runbook_submissions", "opspilot_runbook_revisions"):
                (nullable, default) = (
                    conn.execute(
                        "SELECT is_nullable, column_default FROM information_schema.columns "
                        "WHERE table_name = %s AND column_name = 'kind'",
                        (table,),
                    )
                    .fetchone()
                    .values()
                )
                assert nullable == "NO", table
                assert "'runbook'" in (default or ""), table
            for table in ("opspilot_runbook_submissions", "opspilot_runbook_revisions"):
                checks = [
                    r["def"]
                    for r in conn.execute(
                        "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
                        "WHERE conrelid = %s::regclass AND contype = 'c'",
                        (table,),
                    )
                    if "kind" in r["def"]
                ]
                assert len(checks) == 1, (table, checks)
                assert (
                    "'runbook'" in checks[0] and "'standing_instructions'" in checks[0]
                )
